"""Scoring semantico automatico para CUALQUIER web: una llamada al LLM con el texto de todas las pantallas.

Uso: .venv/bin/python enrich.py <site>
Anade a graph.json: site{name,summary,opening,options} y por nodo: name, wow, wow_why, tags, desc, pitch.
"""
import json, sys
from pathlib import Path

from llm import ask_json

PROMPT = """Eres un experto en demos de producto. Un agente ha rastreado una web que no conoce.
Estas son sus pantallas (id, enlace por el que se llegó, título, encabezados y texto):

{pages}

Analízala para que un agente guía pueda hacer una demo en directo a un visitante. Devuelve este JSON:
{{
 "site": {{
   "name": "nombre del producto/empresa",
   "summary": "qué es y para quién, 1 frase",
   "opening": "primer mensaje del guía al visitante: saludo breve + 1 pregunta para descubrir su rol u objetivo",
   "options": ["3-5 respuestas rápidas típicas del visitante, cortas, en primera persona"]
 }},
 "nodes": {{
   "<id exacto>": {{
     "name": "nombre corto de la pantalla, 1-3 palabras",
     "wow": 0.0-1.0,  // cuánto impresiona enseñarla en una demo: producto en acción, casos reales, resultados, visuales = alto; legal, empleo, kit de prensa, utilidades = bajo
     "wow_why": "por qué impresiona (o no), concreto, máx 12 palabras",
     "tags": ["6-8 temas en minúscula, mezcla español e inglés: funcionalidades, perfiles de comprador, problemas que resuelve"],
     "desc": "qué es esta pantalla, 1 frase concreta",
     "pitch": "lo que diría el guía al enseñarla: 1-2 frases, concretas, con datos de la página si los hay"
   }}
 }}
}}
Incluye TODOS los ids. Responde en español."""


def page_summary(n):
    return json.dumps({"id": n["id"], "link": n.get("label", "")[:60], "title": n.get("title", ""),
                       "headings": n.get("headings", [])[:6], "text": n.get("text", "")[:350]}, ensure_ascii=False)


def enrich(site):
    p = Path(__file__).parent / "data" / site / "graph.json"
    g = json.loads(p.read_text())
    pages = "\n".join(page_summary(n) for n in g["nodes"])
    r = ask_json(PROMPT.format(pages=pages), tier="smart", max_tokens=8000)
    g["meta"] = r["site"]
    for n in g["nodes"]:
        e = r["nodes"].get(n["id"])
        if not e:
            print("sin score:", n["id"])
            n.setdefault("name", n.get("label") or n["id"])
            continue
        n.update(name=e["name"], wow=float(e["wow"]), wow_why=e.get("wow_why", ""), tags=[t.lower() for t in e["tags"]],
                 desc=e["desc"], pitch=e.get("pitch", e["desc"]))
    p.write_text(json.dumps(g, indent=1, ensure_ascii=False))
    print("OK", g["meta"]["name"], "-", g["meta"]["summary"])
    for n in sorted(g["nodes"], key=lambda n: -n["wow"]):
        print(f"  wow {n['wow']:.2f} prior {n['prior']:.2f}  {n['name']:<22} {n.get('wow_why', '')}")


if __name__ == "__main__":
    enrich(sys.argv[1])
