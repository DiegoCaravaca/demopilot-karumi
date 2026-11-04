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
Intereses detectados hasta ahora: {interests}"""

CHAT_PROMPT = """Conversación reciente:
{history}

Último mensaje del visitante: «{msg}»

Devuelve JSON:
{{"reply": "tu respuesta, en español, máximo 3 frases, cercana y concreta. Si pregunta por lo que ve, explícalo con datos de la pantalla. Si aún no sabes su rol u objetivo, haz UNA pregunta concreta.",
 "interests": ["temas que le interesan, en minúscula; usa palabras que aparezcan en los temas de las pantallas"],
 "avoid": ["temas que dice que NO le interesan"],
 "action": "next | goto | auto | pause | stay",
 "target": "id exacto de la pantalla si action=goto, si no null"}}
Reglas para action: si cuenta su objetivo o rol → "next" (o "auto" si pide que le enseñes todo seguido). Si pide ver algo concreto → "goto" con la pantalla que mejor encaje. Si pregunta algo de la pantalla actual → "stay". Si pide parar → "pause"."""

FAST = [(re.compile(r"^\s*(sigue|siguiente|next|continúa|continua|adelante|vale|ok|dale|venga)\W*$", re.I), "next"),
        (re.compile(r"^\s*(para|pausa|stop|espera)\W*$", re.I), "pause"),
        (re.compile(r"^\s*(auto|automático|automatico|modo auto|enséñamelo todo)\W*$", re.I), "auto")]


async def understand(msg):
    p = state["planner"]
    cur = NODES[p.current]
    screens = "\n".join(f"{n['id']} | {n.get('name', '')} | {n.get('desc', '')} | {', '.join(n.get('tags', []))}" for n in GRAPH["nodes"])
    sys_ = CHAT_SYS.format(name=META["name"], summary=META["summary"], screens=screens,
                           current_name=cur.get("name", cur["id"]), current_desc=cur.get("desc", ""),
                           current_text=cur.get("text", "")[:500],
                           visited=", ".join(NODES[v].get("name", v) for v in p.visited),
                           interests=json.dumps(p.tag_w, ensure_ascii=False))
    history = "\n".join(f"{r}: {t}" for r, t in state["chat"][-8:])
    return await asyncio.to_thread(ask_json, CHAT_PROMPT.format(history=history, msg=msg), sys_, "fast", 800)


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
    action = next((a for rx, a in FAST if rx.match(msg)), None)
    reply, target = "", None
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
        if action == "goto" and target in NODES:
            s = state["planner"].goto(target, f"Lo has pedido: «{msg}»")
        elif action == "next":
            s = state["planner"].next()
        shot, url, mode = await do_step(s)
        return snapshot(s, shot, url, mode, reply=reply, action=action)
