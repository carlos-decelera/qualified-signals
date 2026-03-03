import os
import logging
import httpx
import uvicorn
from fastapi import FastAPI, Request, HTTPException

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("AttioSignals")

# ---CONFIGURACION---
ATTIO_API_KEY = os.getenv("ATTIO_API_KEY")
LIST_SLUG = os.getenv("LIST_SLUG")
BASE_URL = "https://api.attio.com/v2"
HEADERS = {
    "Authorization": f"Bearer {ATTIO_API_KEY}",
    "Content-Type": "application/json"
}

app = FastAPI()

# --- FUNCIÓN DE BÚSQUEDA SEGURA (ADIÓS AL INDEX ERROR) ---
def get_value_by_label(questions, label_substring):
    """Busca una pregunta que contenga cierto texto en su título y devuelve el valor."""
    for q in questions:
        label = q.get("label", "")
        if label_substring.lower() in label.lower():
            return q.get("value", "")
    return ""

def validar_tesis_decelera(flags_list):
    signals = {}
    for i in range(1, 8):
        match = next((s for s in flags_list if f"Signal {i}" in s), "")
        signals[i] = match

    gk_ok = all("🟢" in signals.get(i, "") for i in [1, 2, 7])
    comp_greens = sum(1 for i in [3, 4, 5, 6] if "🟢" in signals.get(i, ""))
    comp_ok = comp_greens >= 2

    es_aprobado = gk_ok and comp_ok
    resumen = f"Gatekeepers: {'✅' if gk_ok else '❌'} | Compensadores: {'✅' if comp_ok else '❌'} ({comp_greens}/4)"
    return es_aprobado, resumen

# --- FUNCIONES ATTIO (ESTABLES) ---
async def find_company_id_from_domain(domain: str) -> str:
    url = f"{BASE_URL}/objects/companies/records/query"
    payload = {"filter": {"domains": {"domain": domain}}, "limit": 1}
    async with httpx.AsyncClient(timeout=30.0) as client:
        res = await client.post(url, headers=HEADERS, json=payload)
        data = res.json().get("data", [])
        return data[0].get("id", {}).get("record_id", "") if data else ""

async def find_deal_from_company_id(company_id: str) -> str:
    url = f"{BASE_URL}/objects/deals/records/query"
    payload = {"filter": {"associated_company": {"target_object": "companies", "target_record_id": company_id}}, "limit": 1}
    async with httpx.AsyncClient(timeout=30.0) as client:
        res = await client.post(url, headers=HEADERS, json=payload)
        data = res.json().get("data", [])
        return data[0].get("id", {}).get("record_id", "") if data else ""

async def find_entry_from_deal_id(deal_id: str):
    url = f"{BASE_URL}/lists/{LIST_SLUG}/entries/query"
    payload = {"filter": {"path": [[LIST_SLUG, "parent_record"], ["deals", "record_id"]], "constraints": {"value": deal_id}}, "limit": 1}
    async with httpx.AsyncClient(timeout=30.0) as client:
        res = await client.post(url, headers=HEADERS, json=payload)
        data = res.json().get("data", [])
        if not data: return "", {}
        return data[0].get("id", {}).get("entry_id", ""), data[0].get("entry_values", {})

# --- WEBHOOK ---

@app.post("/webhook")
async def handle_signals(request: Request):
    try:
        form_data = await request.json()
        questions = form_data.get("submission", {}).get("questions", [])
        
        # --- EXTRACCIÓN POR NOMBRE (MÁS ROBUSTO QUE EL ÍNDICE) ---
        reviewer = get_value_by_label(questions, "revisor") or get_value_by_label(questions, "nombre")
        domain = get_value_by_label(questions, "dominio") or get_value_by_label(questions, "web")
        comments_raw = get_value_by_label(questions, "comentario") or get_value_by_label(questions, "feedback")
        
        # Extraemos los signals (esto no da error de índice porque es un bucle sobre lo que existe)
        all_flags_list = []
        for q in questions:
            val = q.get("value")
            # Si es un string con "Signal" o una lista (multi-select)
            if isinstance(val, str) and "Signal" in val:
                all_flags_list.append(val)
            elif isinstance(val, list):
                for item in val:
                    if isinstance(item, str) and "Signal" in item:
                        all_flags_list.append(item)

        # Lógica Decelera
        es_aprobado, resumen_tesis = validar_tesis_decelera(all_flags_list)

        # Flujo Attio
        company_id = await find_company_id_from_domain(domain)
        deal_id = await find_deal_from_company_id(company_id)
        entry_id, entry_values = await find_entry_from_deal_id(deal_id)

        if not entry_id: return {"status": "error", "message": "Entry not found"}

        tier_list = entry_values.get("tier_5", [])
        tier_actual = tier_list[0].get("status", {}).get("title", "Tier 1") if tier_list else "Tier 1"
        
        t1_ok = len(entry_values.get("tier_1_ok", []))
        t1_ko = len(entry_values.get("tier_1_ko", []))
        t2_ok = len(entry_values.get("tier_2_ok", []))
        t2_ko = len(entry_values.get("tier_2_ko", []))

        if tier_actual == "Tier 1":
            if es_aprobado: t1_ok += 1
            else: t1_ko += 1
        else:
            if es_aprobado: t2_ok += 1
            else: t2_ko += 1

        # Construir Payload
        voto_label = "✅ OK" if es_aprobado else "🔴 KO"
        nuevo_p = f"{reviewer} ({voto_label}):\n{resumen_tesis}\n" + "\n".join(all_flags_list)
        new_g = f"{reviewer}: " + " ".join([f for f in all_flags_list if "🟢" in f])
        new_r = f"{reviewer}: " + " ".join([f for f in all_flags_list if "🔴" in f or "🟡" in f])

        def concat(nuevo, field):
            ex = entry_values.get(field, [{}])[0].get("value", "")
            return f"{nuevo}\n---\n{ex}" if ex else nuevo

        final_attio_data = {
            "signals_qualified": [{"value": concat(nuevo_p, "signals_qualified")}],
            "green_flags_qualified": [{"value": concat(new_g, "green_flags_qualified")}],
            "red_flags_qualified": [{"value": concat(new_r, "red_flags_qualified")}],
            "status": [{"status": (await find_status(tier_actual, t1_ok, t1_ko, t2_ok, t2_ko, entry_values))[0]}]
        }

        # Comentario
        if comments_raw:
            new_comm = f"{reviewer}: {comments_raw}"
            final_attio_data["signals_comments_qualified"] = [{"value": concat(new_comm, "signals_comments_qualified")}]

        # Marcar voto
        v_field = (f"tier_{'1' if tier_actual == 'Tier 1' else '2'}_{'ok' if es_aprobado else 'ko'}")
        final_attio_data[v_field] = [{"option": reviewer}]

        # Patch
        async with httpx.AsyncClient(timeout=30.0) as client:
            await client.patch(f"{BASE_URL}/lists/{LIST_SLUG}/entries/{entry_id}", headers=HEADERS, json={"data": {"entry_values": final_attio_data}})
        
        return {"status": "success"}

    except Exception as e:
        logger.error(f"Error: {e}")
        return {"status": "error", "detail": str(e)}

async def find_status(tier, t1ok, t1ko, t2ok, t2ko, ev):
    current = ev.get("status", [{}])[0].get("status", {}).get("title", "Initial screening")
    if tier == "Tier 2" or (t1ok >= 1 and t1ko >= 1):
        if t2ok >= 2: return "First interaction", True
        if t2ko >= 2: return "Killed", False
        return current, True
    if t1ok >= 2: return "First interaction", True
    if t1ko >= 2: return "Killed", False
    return current, True

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))