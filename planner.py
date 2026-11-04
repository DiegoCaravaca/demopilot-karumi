"""Planner: decide la siguiente pantalla de la demo. Determinista e instantaneo (el LLM entiende, el planner decide).

score = 0.35*prior + 0.30*wow + 0.45*interes - 0.06*(saltos extra)
- core: lo principal de la web (facil de encontrar + wow). Siempre se cubre.
- Sin presupuesto fijo: para cuando el core esta cubierto y lo que queda ya no aporta.
"""
from collections import deque

W = {"prior": 0.35, "wow": 0.30, "interest": 0.45, "dist": 0.06, "repeat": 0.30}
CORE_K, STOP_SCORE, MAX_STEPS = 3, 0.55, 7


class Planner:
    def __init__(self, graph, emb=None):
        self.nodes = {n["id"]: n for n in graph["nodes"]}
        self.adj = {nid: set() for nid in self.nodes}
        for e in graph["edges"]:  # no dirigido: siempre se puede volver atras
            self.adj[e["from"]].add(e["to"])
            self.adj[e["to"]].add(e["from"])
        self.root = graph["root"]
        rank = sorted(graph["nodes"], key=lambda n: -(0.6 * n["prior"] + 0.4 * n["wow"]))
        self.core = [n["id"] for n in rank if n["id"] != self.root][:CORE_K]
        self.tag_w = {}   # interes del visitante: tema -> peso (+ gusta, - no gusta)
        self.node_fb = {}  # feedback directo sobre pantallas
        self.current = self.root
        self.visited = [self.root]
        self.log = []
        self.emb = emb    # {id: vector} embeddings de las pantallas
        self.profile = None  # vector de lo que busca el visitante
        self.sem = {}     # similitud semantica normalizada 0..1 por pantalla

    # ---------- objetivo y feedback (vienen del LLM o de los botones) ----------
    def add_interests(self, terms, value=1.0):
        for t in terms:
            t = t.strip().lower()
            if t:
                self.tag_w[t] = round(max(-2.0, min(2.0, self.tag_w.get(t, 0) + value)), 2)

    def feedback(self, nid, value):
        """+1 me mola / -1 no me interesa: refuerza o penaliza los temas de esa pantalla."""
        self.add_interests(self.nodes[nid].get("tags", [])[:4], 0.5 * value)
        self.node_fb[nid] = self.node_fb.get(nid, 0) + 0.3 * value

    def update_profile(self, vec, weight=1.0):
        """Suma al perfil semantico del visitante (mensajes +, feedback +/-) y recalcula similitudes."""
        if self.emb is None or vec is None:
            return
        self.profile = weight * vec if self.profile is None else self.profile + weight * vec
        raw = {k: float(v @ self.profile) for k, v in self.emb.items()}
        lo, hi = min(raw.values()), max(raw.values())
        self.sem = {k: (x - lo) / (hi - lo or 1) for k, x in raw.items()}

    # ---------- piezas del score ----------
    def text(self, n):
        return " ".join([n["id"], n.get("name", ""), n.get("desc", ""), " ".join(n.get("tags", []))]).lower()

    def interest(self, n):
        txt = self.text(n)
        pos = {t: w for t, w in self.tag_w.items() if w > 0 and t in txt}
        neg = {t: w for t, w in self.tag_w.items() if w < 0 and t in txt}
        total_pos = sum(w for w in self.tag_w.values() if w > 0)
        tag_s = min(1.0, sum(pos.values()) / max(1.0, 0.6 * total_pos)) if total_pos else 0.0
        sem = self.sem.get(n["id"])
        # temas exactos (explicables) + significado (sinonimos, idiomas): mitad y mitad
        base = tag_s if sem is None else (0.5 * tag_s + 0.5 * sem if total_pos else sem)
        s = base + sum(neg.values()) * 0.5 + self.node_fb.get(n["id"], 0)
        return max(-1.0, min(1.0, s)), sorted(pos, key=lambda t: -pos[t]), sorted(neg)

    def path(self, a, b):
        prev, q = {a: None}, deque([a])
        while q:
            x = q.popleft()
            if x == b:
                break
            for y in self.adj[x]:
                if y not in prev:
                    prev[y] = x
                    q.append(y)
        if b not in prev:
            return [a, b]
        out = [b]
        while out[-1] != a:
            out.append(prev[out[-1]])
        return out[::-1]

    def overlap(self, n):
        """Cuanto se parece a algo ya ensenado (Jaccard de temas): evita repetir 4 demos iguales."""
        a, best, like = set(n.get("tags", [])), 0.0, None
        for v in self.visited[1:]:
            if v == n["id"]:
                continue
            b = set(self.nodes[v].get("tags", []))
            j = len(a & b) / max(1, len(a | b))
            if j > best:
                best, like = j, v
        return best, like

    def breakdown(self, nid):
        n = self.nodes[nid]
        inter, hits, miss = self.interest(n)
        hops = len(self.path(self.current, nid)) - 1
        rep, like = self.overlap(n)
        parts = {"prior": W["prior"] * n["prior"], "wow": W["wow"] * n["wow"],
                 "interest": W["interest"] * inter, "dist": -W["dist"] * max(hops - 1, 0), "repeat": -W["repeat"] * rep}
        return {"id": nid, "name": n.get("name", nid), "score": round(sum(parts.values()), 3),
                "parts": {k: round(v, 3) for k, v in parts.items()},
                "raw": {"prior": n["prior"], "wow": n["wow"], "interest": round(inter, 2), "hops": hops,
                        "repeat": round(rep, 2), "sem": round(self.sem[nid], 2) if nid in self.sem else None, "like": self.nodes[like].get("name", like) if like else None},
                "hits": hits, "miss": miss, "core": nid in self.core}

    # ---------- estado ----------
    def candidates(self):
        return [nid for nid in self.nodes if nid not in self.visited]

    def ranking(self, k=6):
        return sorted((self.breakdown(c) for c in self.candidates()), key=lambda b: -b["score"])[:k]

    def core_left(self):
        return [c for c in self.core if c not in self.visited]

    def done(self):
        steps = len(self.visited) - 1
        if steps >= MAX_STEPS or not self.candidates():
            return True
        best = self.ranking(1)
        return not self.core_left() and (not best or best[0]["score"] < STOP_SCORE)

    # ---------- moverse ----------
    def next(self):
        if self.done():
            return None
        ranking = self.ranking(len(self.nodes))
        core_left = self.core_left()
        # cobertura: si lo que queda de margen es justo el core pendiente, se fuerza el core
        forced = core_left and (MAX_STEPS - (len(self.visited) - 1)) <= len(core_left)
        # sin impacto (legal, empleo, blog...) solo si el visitante lo busca de verdad
        worth = [b for b in ranking if b["core"] or b["raw"]["wow"] >= 0.4 or (b["raw"]["interest"] >= 0.8 and b["hits"])] or ranking
        best = next(b for b in worth if (not forced or b["core"]))
        # si el mejor no-core no aporta nada, mejor cubrir el core
        if not forced and core_left and best["score"] < STOP_SCORE and not best["core"]:
            best, forced = next(b for b in ranking if b["core"]), True
        return self._go(best, "core" if forced else "score", ranking)

    def goto(self, nid, reason="Lo has pedido tú"):
        b = self.breakdown(nid)
        return self._go(b, "request", self.ranking(len(self.nodes)), reason)

    def _go(self, b, mode, ranking, reason=None):
        nid = b["id"]
        path = self.path(self.current, nid)
        alts = [r for r in ranking if r["id"] != nid][:2]
        step = {"node": nid, "name": b["name"], "path": path, "mode": mode, "breakdown": b,
                "why": self.explain(b, mode, alts, reason), "pitch": self.nodes[nid].get("pitch", self.nodes[nid].get("desc", ""))}
        self.current = nid
        if nid not in self.visited:
            self.visited.append(nid)
        self.log.append(step)
        return step

    def explain(self, b, mode, alts, reason):
        n, r, why = self.nodes[b["id"]], b["raw"], []
        if mode == "request":
            why.append(reason)
        elif mode == "core":
            why.append(f"Cobertura: es una de las {len(self.core)} pantallas clave y aún no la has visto")
        if b["hits"]:
            why.append(f"Encaja con lo que buscas: «{'», «'.join(b['hits'][:3])}» (interés {r['interest']:.2f})")
        if r.get("sem") is not None and r["sem"] >= 0.6:
            why.append(f"Por significado se parece a lo que has contado: similitud {r['sem']:.2f} (embedding)")
        if b["miss"]:
            why.append(f"Ojo: toca temas que no te interesan ({', '.join(b['miss'])})")
        where = [f"a {n['depth']} clic{'s' if n['depth'] != 1 else ''} de la portada"]
        if n.get("in_nav") and n.get("nav_pos", 99) < 99:
            where.append(f"nº {n['nav_pos'] + 1} del menú")
        if n.get("global"):
            where.append("enlazada desde toda la web")
        why.append(f"Fácil de encontrar ({', '.join(where)}) → prioridad de la empresa {r['prior']:.2f}")
        why.append(f"Impacto visual {r['wow']:.2f}: {n.get('wow_why') or n.get('desc', '')}")
        if r["repeat"] >= 0.3:
            why.append(f"Se parece un {r['repeat']:.0%} a «{r['like']}», ya enseñada (penaliza)")
        if alts:
            a = alts[0]
            why.append(f"Le gana a «{a['name']}» por {b['score'] - a['score']:+.2f}")
        return why
