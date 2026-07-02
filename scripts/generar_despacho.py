#!/usr/bin/env python3
"""
Genera la edición diaria de El Despacho.

Corre en GitHub Actions (internet abierto). Pasos:
  1. Determina la fecha de hoy en horario de Chile y el nº de edición.
  2. Llama a la API de Anthropic (Claude + web_search) para redactar las
     noticias del día como JSON, ordenadas por importancia, parafraseadas.
  3. Para cada nota, baja el og:image del artículo fuente por hotlink
     (nunca se descarga la imagen: solo se guarda su URL).
  4. Escribe data/AAAA-MM-DD.json y actualiza data/index.json.

El workflow se encarga del commit + push a main.
"""
import json
import os
import re
import sys
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import anthropic

MODEL = "claude-opus-4-8"  # cambiá a "claude-sonnet-4-6" para abaratar
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36")

DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
         "agosto", "septiembre", "octubre", "noviembre", "diciembre"]

PROMPT = """Sos el editor de "El Despacho", un despacho diario de noticias para un lector que vive en Santiago de Chile (para él, "nacional" = Chile).

Usá la herramienta de búsqueda web para encontrar las noticias MÁS IMPORTANTES de las últimas 24 horas, en dos secciones: internacional y Chile. Incluí TODAS las que sean genuinamente relevantes (sin límite fijo de cantidad), ordenadas por importancia.

Devolvé EXCLUSIVAMENTE un bloque de código JSON (```json ... ```), sin texto antes ni después, con esta forma exacta:

{
  "intl": {
    "notas": [
      {"eyebrow": "categoría corta", "title": "titular", "dek": "bajada de 1-2 líneas", "more": "contexto adicional para 'Leer más'", "source": "URL del artículo original"}
    ],
    "briefs": [
      {"eyebrow": "categoría corta", "title": "titular", "dek": "bajada breve", "source": "URL del artículo original"}
    ]
  },
  "chile": { "notas": [ ... ], "briefs": [ ... ] }
}

Reglas:
- "notas" = noticias mayores/medianas (la primera de cada sección es la apertura, la más importante). "briefs" = noticias chicas de una línea.
- Nunca copies texto textual de los artículos: parafraseá siempre, en español neutro.
- "source" debe ser la URL directa del artículo fuente (no la home del medio).
- Incluí "more" siempre que tengas contexto real para agregar; si no, omitilo.
- No inventes noticias. No incluyas ningún campo de imagen (las imágenes se agregan aparte)."""


def fecha_chile():
    return datetime.now(ZoneInfo("America/Santiago")).date()


def date_label(d):
    return f"{DIAS[d.weekday()].capitalize()} {d.day} de {MESES[d.month - 1]} de {d.year}"


def cargar_manifest():
    p = DATA / "index.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {"latest": None, "dates": []}


def og_image(url):
    """Devuelve la URL del og:image del artículo, o None."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            charset = r.headers.get_content_charset() or "utf-8"
            html = r.read(400_000).decode(charset, "ignore")
    except Exception as e:
        print(f"  · sin imagen ({url}): {e}", file=sys.stderr)
        return None
    for pat in (
        r'<meta[^>]+property=["\']og:image(?::url)?["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image(?::url)?["\']',
        r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)',
    ):
        m = re.search(pat, html, re.I)
        if m:
            return m.group(1).strip()
    return None


def extraer_json(texto):
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", texto, re.S)
    bruto = m.group(1) if m else texto[texto.find("{"): texto.rfind("}") + 1]
    return json.loads(bruto)


def pedir_noticias():
    client = anthropic.Anthropic()  # ANTHROPIC_API_KEY del entorno
    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 10}]
    messages = [{"role": "user", "content": PROMPT}]
    textos = []
    for _ in range(6):  # tolera pause_turn del loop de búsqueda del servidor
        resp = client.messages.create(
            model=MODEL, max_tokens=16000, tools=tools, messages=messages,
        )
        textos += [b.text for b in resp.content if b.type == "text"]
        if resp.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": resp.content})
            continue
        break
    return extraer_json("\n".join(textos))


def main():
    d = fecha_chile()
    ymd = d.isoformat()
    manifest = cargar_manifest()
    edition = f"{len(manifest['dates']) + (0 if ymd in manifest['dates'] else 1):03d}"

    print(f"Generando edición {edition} para {ymd}…")
    data = pedir_noticias()

    secciones = {
        "intl": {"label": "Internacional", "wire": "world wire"},
        "chile": {"label": "Chile", "wire": "santiago wire"},
    }
    out = {"date": ymd, "dateLabel": date_label(d), "edition": edition, "sections": {}}
    for key, meta in secciones.items():
        sec = data.get(key, {})
        notas = sec.get("notas", []) or []
        briefs = sec.get("briefs", []) or []
        print(f"[{key}] {len(notas)} notas, {len(briefs)} breves — buscando imágenes…")
        for n in notas:
            img = og_image(n["source"]) if n.get("source") else None
            if img:
                n["image"] = img
        out["sections"][key] = {
            "label": meta["label"], "wire": meta["wire"],
            "notas": notas, "briefs": briefs,
        }

    (DATA / f"{ymd}.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    if ymd not in manifest["dates"]:
        manifest["dates"].append(ymd)
    manifest["dates"] = sorted(set(manifest["dates"]))
    manifest["latest"] = ymd
    (DATA / "index.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"✅ Escrito data/{ymd}.json y actualizado data/index.json (latest={ymd}).")


if __name__ == "__main__":
    main()
