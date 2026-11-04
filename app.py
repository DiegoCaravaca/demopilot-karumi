"""DemoPilot: chat + navegador en vivo + grafo. El LLM entiende (objetivo, feedback, ordenes), el planner decide.

Uso: SITE=karumi LIVE=1 .venv/bin/uvicorn app:app --port 8000
"""
import asyncio, base64, json, os, re
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from llm import ask_json, backend
from planner import Planner
from embed import embed, node_text

ROOT = Path(__file__).parent
SITE = os.environ.get("SITE", "karumi")
LIVE = os.environ.get("LIVE", "1") == "1"
DATA = ROOT / "data" / SITE
GRAPH = json.loads((DATA / "graph.json").read_text())
CFG = json.loads((ROOT / "sites.json").read_text())[SITE]
NODES = {n["id"]: n for n in GRAPH["nodes"]}
META = GRAPH.get("meta", {"name": SITE, "summary": "", "opening": "¡Hola! ¿Qué te gustaría ver?", "options": []})

app = FastAPI()
app.mount("/data", StaticFiles(directory=DATA), name="data")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
print("LLM backend:", backend())
IDS = [n["id"] for n in GRAPH["nodes"]]
EMB = dict(zip(IDS, embed(node_text(n) for n in GRAPH["nodes"])))  # ~1 s al arrancar
state = {"planner": Planner(GRAPH, EMB), "page": None, "pw": None, "chat": [], "lock": asyncio.Lock()}


# ---------------- navegador ----------------
async def get_page():
    if not LIVE:
        return None
    if state["page"] is None:
        try:
            from playwright.async_api import async_playwright
            from crawler import login
            state["pw"] = await async_playwright().start()
            browser = await state["pw"].chromium.launch()
            page = await browser.new_page(viewport={"width": 1280, "height": 800})
            if CFG.get("login"):
                await login(page, CFG["login"])
            state["page"] = page
        except Exception as e:
            print("LIVE desactivado:", e)
            return None
    return state["page"]


async def visit(nid):
    from crawler import goto
    n = NODES[nid]
    page = await get_page()
    if page is not None:
        try:
            await goto(page, n["url"], settle=500)
            png = await page.screenshot(type="jpeg", quality=75)
            return "data:image/jpeg;base64," + base64.b64encode(png).decode(), page.url, "live"
        except Exception as e:
            print("live fallo, uso cache:", e)
    return f"/data/{n['shot']}", n["url"], "replay"


async def do_step(step):
    shot = url = mode = None
    if step:
        for hop in step["path"][1:]:  # recorre el camino real, salto a salto
            shot, url, mode = await visit(hop)
    return shot, url, mode


CLICKABLE_JS = """() => {
  const out = [];
  document.querySelectorAll('[data-dp]').forEach(e => e.removeAttribute('data-dp'));
  [...document.querySelectorAll('a, button, [role=button], [role=tab], [role=menuitem], summary')].forEach((e, i) => {
    const r = e.getBoundingClientRect();
    const t = (e.innerText || e.getAttribute('aria-label') || '').trim().replace(/\\s+/g, ' ').slice(0, 60);
    if (t && r.width > 0 && r.height > 0) { e.setAttribute('data-dp', i); out.push({ i, t }); }
  });
  return out;
}"""


async def clickables():
    page = await get_page()
    if page is None:
        return []
    try:
        return await page.evaluate(CLICKABLE_JS)
    except Exception:
        return []


def best_match(query, items):
    """Texto exacto > contiene > parecido por embedding. 'see open roles' -> 'See the open roles'."""
    q = query.lower().strip(" \"'«».")
    for it in items:
        if it["t"].lower() == q:
            return it
    subs = [it for it in items if len(it["t"]) > 2 and (q in it["t"].lower() or it["t"].lower() in q)]
    if subs:
        return min(subs, key=lambda it: abs(len(it["t"]) - len(q)))
    words = set(re.findall(r"\w{3,}", q))
    scored = sorted(items, key=lambda it: -len(words & set(re.findall(r"\w{3,}", it["t"].lower()))))
    if scored and len(words & set(re.findall(r"\w{3,}", scored[0]["t"].lower()))) >= max(1, len(words) - 1):
        return scored[0]
    return None


def llm_pick(query, items):
    """Sin coincidencia de texto: el LLM elige entre los botones REALES de la pagina (o ninguno)."""
    texts = list(dict.fromkeys(it["t"] for it in items))[:80]
    try:
        r = ask_json(f"El visitante quiere pulsar: «{query}». Botones y enlaces visibles en la página:\n"
                     + "\n".join(f"- {t}" for t in texts)
                     + '\nDevuelve {"text": "texto EXACTO del que mejor encaja, o null si ninguno encaja de verdad"}', tier="fast", max_tokens=2000)
        t = r.get("text")
        return next((it for it in items if it["t"] == t), None)
    except Exception as e:
        print("llm_pick fallo:", e)
        V = embed([query] + [it["t"] for it in items])
        sims = V[1:] @ V[0]
        i = int(sims.argmax())
        return items[i] if sims[i] > 0.7 else None


async def after_click(label):
    """Tras pulsar: captura, y si hemos llegado a una pantalla del mapa, el planner se entera."""
    from crawler import dismiss_overlays, normalize
    page = state["page"]
    await page.wait_for_timeout(1300)
    await dismiss_overlays(page)
    p, step = state["planner"], None
    nid = normalize(page.url)
    if nid in NODES and nid != p.current:
        step = p.goto(nid, f"Has pulsado «{label}»")
    png = await page.screenshot(type="jpeg", quality=75)
    shot = "data:image/jpeg;base64," + base64.b64encode(png).decode()
    return snapshot(step, shot, page.url, "live", clicked=label, off_map=nid not in NODES)


async def click_text(query):
    items = await clickables()
    if not items:
        return snapshot(reply="Para pulsar botones necesito el modo en directo (LIVE=1).")
    it = best_match(query, items) or await asyncio.to_thread(llm_pick, query, items)
    if it is None:
        return snapshot(reply=f"No encuentro nada parecido a «{query}» en esta pantalla. Puedo pulsar: " + ", ".join(x["t"] for x in items[:8]))
    try:
        await state["page"].click(f'[data-dp="{it["i"]}"]', timeout=4000)
    except Exception:
        await state["page"].locator(f'[data-dp="{it["i"]}"]').evaluate("e => e.click()")
    return await after_click(it["t"])


def snapshot(step=None, shot=None, url=None, mode=None, **extra):
    p = state["planner"]
    return {"step": step, "shot": shot, "url": url, "mode": mode, "visited": p.visited, "current": p.current,
            "core": p.core, "core_left": p.core_left(), "ranking": p.ranking(5), "done": p.done(),
            "interests": p.tag_w, **extra}


# ---------------- LLM: chat ----------------
CHAT_SYS = """Eres DemoPilot, un guía que hace demos en directo de «{name}» ({summary}).
Hablas con un visitante mientras navegas la web por él. Tu trabajo: entender qué busca, explicarle lo que ve y decidir hacia dónde moverte.

Pantallas disponibles (id | nombre | qué es | temas):
{screens}

Pantalla actual: {current_name} — {current_desc}
Texto visible en ella: {current_text}
Ya enseñadas: {visited}
Intereses detectados hasta ahora: {interests}
Botones y enlaces que puedes pulsar en la pantalla actual: {clicks}"""

CHAT_PROMPT = """Conversación reciente:
{history}

Último mensaje del visitante: «{msg}»

Devuelve JSON:
{{"reply": "tu respuesta, en español, máximo 3 frases, cercana y concreta. Si pregunta por lo que ve, explícalo con datos de la pantalla. Si aún no sabes su rol u objetivo, haz UNA pregunta concreta.",
 "interests": ["temas que le interesan, en minúscula; usa palabras que aparezcan en los temas de las pantallas"],
 "avoid": ["temas que dice que NO le interesan"],
 "action": "next | goto | click | auto | pause | stay",
 "target": "id exacto de la pantalla si action=goto; texto exacto del botón si action=click; si no null",
 "then_click": "si pide ir a una pantalla Y pulsar algo allí (p.ej. 'llévame a empleo y abre las ofertas'), texto aproximado de ese botón; si no null"}}
Reglas para action: si cuenta su objetivo o rol → "next" (o "auto" si pide que le enseñes todo seguido). Si pide ver algo concreto → "goto" con la pantalla que mejor encaje. Si pide pulsar/abrir/ver un botón o enlace de la pantalla actual → "click" con su texto. Si pregunta algo de la pantalla actual → "stay". Si pide parar → "pause"."""

FAST = [(re.compile(r"^\s*(sigue|siguiente|next|continúa|continua|adelante|vale|ok|dale|venga)\W*$", re.I), "next"),
        (re.compile(r"^\s*(para|pausa|stop|espera)\W*$", re.I), "pause"),
        (re.compile(r"^\s*(auto|automático|automatico|modo auto|enséñamelo todo)\W*$", re.I), "auto")]
CLICK_RX = re.compile(r"^\s*(pincha|pulsa|haz clic|haz click|clica|click|clic|dale a|abre)\s+(en\s+|a\s+|el\s+|la\s+)*(.+)$", re.I)


async def understand(msg):
    p = state["planner"]
    cur = NODES[p.current]
    screens = "\n".join(f"{n['id']} | {n.get('name', '')} | {n.get('desc', '')} | {', '.join(n.get('tags', []))}" for n in GRAPH["nodes"])
    sys_ = CHAT_SYS.format(name=META["name"], summary=META["summary"], screens=screens,
                           current_name=cur.get("name", cur["id"]), current_desc=cur.get("desc", ""),
                           current_text=cur.get("text", "")[:500],
                           visited=", ".join(NODES[v].get("name", v) for v in p.visited),
                           interests=json.dumps(p.tag_w, ensure_ascii=False),
                           clicks=" | ".join(dict.fromkeys(x["t"] for x in await clickables()))[:1500] or "(modo replay: ninguno)")
    history = "\n".join(f"{r}: {t}" for r, t in state["chat"][-8:])
    return await asyncio.to_thread(ask_json, CHAT_PROMPT.format(history=history, msg=msg), sys_, "fast", 3000)


# ---------------- API ----------------
class ChatReq(BaseModel):
    message: str


class FbReq(BaseModel):
    value: int


class GotoReq(BaseModel):
    node: str


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/graph")
def graph():
    return {**GRAPH, "meta": META, "site_key": SITE, "weights": __import__("planner").W}


@app.post("/api/start")
async def start():
    async with state["lock"]:
        state["planner"], state["chat"] = Planner(GRAPH, EMB), [("guía", META["opening"])]
        shot, url, mode = await visit(GRAPH["root"])
        return snapshot(None, shot, url, mode, reply=META["opening"], options=META.get("options", []))


@app.post("/api/step")
async def step():
    async with state["lock"]:
        s = state["planner"].next()
        shot, url, mode = await do_step(s)
        if s:
            state["chat"].append(("guía", f"[enseña {s['name']}] {s['pitch']}"))
        return snapshot(s, shot, url, mode)


@app.post("/api/goto")
async def goto_node(req: GotoReq):
    async with state["lock"]:
        s = state["planner"].goto(req.node, "Has hecho clic en el mapa")
        shot, url, mode = await do_step(s)
        return snapshot(s, shot, url, mode)


class ClickReq(BaseModel):
    text: str = ""
    x: float = -1
    y: float = -1


@app.post("/api/click")
async def click(req: ClickReq):
    """Pulsar por texto o por coordenadas (clic directo sobre la captura, x/y en 0..1)."""
    async with state["lock"]:
        if req.text:
            return await click_text(req.text)
        page = await get_page()
        if page is None:
            return snapshot(reply="Para pulsar en la web necesito el modo en directo (LIVE=1).")
        vp = page.viewport_size
        x, y = req.x * vp["width"], req.y * vp["height"]
        label = await page.evaluate("""([x, y]) => { const e = document.elementFromPoint(x, y);
            const c = e && (e.closest('a,button,[role=button],[role=tab],summary') || e);
            return c ? (c.innerText || c.getAttribute('aria-label') || c.tagName).trim().replace(/\\s+/g,' ').slice(0, 50) : '' }""", [x, y])
        await page.mouse.click(x, y)
        return await after_click(label or "un punto de la página")


@app.post("/api/feedback")
async def feedback(req: FbReq):
    p = state["planner"]
    p.feedback(p.current, req.value)
    p.update_profile(EMB[p.current], 0.7 * req.value)  # 👍 acerca el perfil a esta pantalla, 👎 lo aleja
    state["chat"].append(("visitante", "👍 me gusta esto" if req.value > 0 else "👎 esto no me interesa"))
    return snapshot()


@app.post("/api/chat")
async def chat(req: ChatReq):
    msg = req.message.strip()
    state["chat"].append(("visitante", msg))
    m = CLICK_RX.match(msg)
    if m:  # via rapida: "pincha en see open roles" sin pasar por el LLM
        async with state["lock"]:
            return await click_text(m.group(3))
    action = next((a for rx, a in FAST if rx.match(msg)), None)
    reply, target, r = "", None, {}
    if action is None:
        try:
            r = await understand(msg)
        except Exception as e:
            print("LLM fallo:", e)
            r = {"reply": "Perdona, no te he entendido bien. ¿Me lo dices de otra forma?", "action": "stay"}
        p = state["planner"]
        if r.get("action") != "stay":  # lo que cuenta de si mismo (no preguntas sueltas) entra en su perfil
            p.update_profile(embed([msg + " " + " ".join(r.get("interests", []))])[0])
        if r.get("avoid"):
            p.update_profile(embed([" ".join(r["avoid"])])[0], -0.5)
        p.add_interests(r.get("interests", []), 1.0)
        p.add_interests(r.get("avoid", []), -1.0)
        reply, action, target = r.get("reply", ""), r.get("action", "stay"), r.get("target")
        state["chat"].append(("guía", reply))
    async with state["lock"]:
        s = None
        if action == "click" and target:
            return {**(await click_text(target)), "reply": reply, "action": action}
        if action == "goto" and target in NODES:
            s = state["planner"].goto(target, f"Lo has pedido: «{msg}»")
        elif action == "next":
            s = state["planner"].next()
        shot, url, mode = await do_step(s)
        if s and r.get("then_click") and mode == "live":  # ir y luego pulsar, en un solo mensaje
            return {**(await click_text(r["then_click"])), "step": s, "reply": reply, "action": action}
        return snapshot(s, shot, url, mode, reply=reply, action=action)
