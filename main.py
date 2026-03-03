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

# Tus constantes originales
REVIEWER_INDEX = 0
DOMAIN_INDEX = 1
FLAGS_START = 2
FLAGS_END = 9
MULTI_FLAGS_START = 9
MULTI_FLAGS_END = 11
COMMENTS_INDEX = 11

app = FastAPI()

# --- NUEVA LÓGICA DE VALIDACIÓN (LA FOTO) ---

def aplicar_tesis_decelera(flags_list):
    """
    Recibe la lista de strings (ej: ['Signal 1 -> 🟢', 'Signal 2 -> 🔴', ...])
    y decide si es un pase o un KO según la imagen.
    """
    # 1. Convertimos la lista en un diccionario para buscar fácil
    # Buscamos si el string contiene "Signal X" y qué emoji tiene
    d = {}
    for i in range(1, 8):
        # Buscamos el emoji para la señal i
        found = next((f for f in flags_list if f"Signal {i}" in f), "")
        d[i] = found

    # 2. Gatekeepers (S1, S2, S7): 3 Verdes obligatorios. Rojo/Amarillo = KO.
    # Si alguno NO tiene el verde, es KO.
    gk_ok = all("🟢" in d.get(i, "") for i in [1, 2, 7])

    # 3. Compensadores (S3, S4, S5, S6): Mínimo 2 Verdes.
    comp_greens = sum(1 for i in [3, 4, 5, 6] if "🟢" in d.get(i, ""))
    comp_ok = comp_greens >= 2

    es_aprobado = gk_ok and comp_ok

    # Creamos un resumen visual para el historial
    resumen = f"Gatekeepers: {'✅' if gk_ok else '❌'} | Compensadores: {'✅' if comp_ok else '❌'} ({comp_greens}/4)"
    
    return es_aprobado, resumen

# --- FUNCIONES AUXILIARES ORIGINALES ---

async def find_company_id_from_domain(domain: str) -> str:
    url = f"{BASE_URL}/objects/companies/records/query"
    payload = {"filter": {"domains": {"domain": domain}}, "limit": 1}
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            res = await client.post(url, headers=HEADERS, json=payload)
            res.raise_for_status()
            data = res.json().get("data", [])
            return data[0].get("id", {}).get("record_id", "") if data else ""
        except Exception as e:
            logger.error(f"Error buscando compañía: {e}")
            return ""

async def find_deal_from_company_id(company_id: str) -> str:
    url = f"{BASE_URL}/objects/deals/records/query"
    payload = {"filter": {"associated_company": {"target_object": "companies", "target_record_id": company_id}}, "limit": 1}
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            res = await client.post(url, headers=HEADERS, json=payload)
            res.raise_for_status()
            data = res.json().get("data", [])
            return data[0].get("id", {}).get("record_id", "") if data else ""
        except Exception as e:
            logger.error(f"Error buscando deal: {e}")
            return ""

async def find_entry_from_deal_id(deal_id: str):
    url = f"{BASE_URL}/lists/{LIST_SLUG}/entries/query"
    payload = {"filter": {"path": [[LIST_SLUG, "parent_record"], ["deals", "record_id"]], "constraints": {"value": deal_id}}, "limit": 1}
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            res = await client.post(url, headers=HEADERS, json=payload)
            res.raise_for_status()
            data = res.json().get("data", [])
            if not data: return "", {}
            return data[0].get("id", {}).get("entry_id", ""), data[0].get("entry_values", {})
        except Exception as e:
            logger.error(f"Error buscando entry: {e}")
            return "", {}

def calculate_funnel_status(tier_actual, t1_ok, t1_ko, t2_ok, t2_ko, default_status=None):
    if tier_actual == "Tier 2" or (t1_ok >= 1 and t1_ko >= 1):
        if t2_ok >= 2: return "First interaction", True
        if t2_ko >= 2: return "Killed", False
        return default_status, True
    if t1_ok >= 2: return "First interaction", True
    if t1_ko >= 2: return "Killed", False
    return default_status if default_status else "Initial screening", True

async def upload_attio_entry(entry_id, payload_values):
    url = f"{BASE_URL}/lists/{LIST_SLUG}/entries/{entry_id}"
    data = {"data": {"entry_values": payload_values}}
    async with httpx.AsyncClient(timeout=30.0) as client:
        res = await client.patch(url, headers=HEADERS, json=data)
        res.raise_for_status()

# --- WEBHOOK PRINCIPAL ---

@app.post("/webhook")
async def handle_signals(request: Request):
    try:
        form_data = await request.json()
        questions = form_data.get("submission", {}).get("questions", [])
        
        # Mantenemos tu lógica de extracción original exacta
        reviewer = questions[REVIEWER_INDEX].get("value", "")
        domain = questions[DOMAIN_INDEX].get("value", "")
        comments_raw = questions[COMMENTS_INDEX].get("value", "")
        new_comment = f"{reviewer}: {comments_raw}" if comments_raw else ""

        all_flags_list = []
        # Tu bucle original de flags
        for q in questions[FLAGS_START:FLAGS_END]:
            val = q.get("value", "")
            if val: all_flags_list.append(val)
        
        # Tu bucle original de multi-flags
        for q in questions[MULTI_FLAGS_START:MULTI_FLAGS_END]:
            val = q.get("value")
            if isinstance(val, list):
                for item in val: all_flags_list.append(item)

        # APLICAMOS LA LÓGICA DE LA FOTO SOBRE LA LISTA EXTRAÍDA
        es_aprobado, resumen_tesis = aplicar_tesis_decelera(all_flags_list)

        # Buscar IDs en Attio
        company_id = await find_company_id_from_domain(domain)
        deal_id = await find_deal_from_company_id(company_id)
        entry_id, entry_values = await find_entry_from_deal_id(deal_id)

        if not entry_id: raise HTTPException(status_code=404, detail="Entry no encontrada")

        # Conteo de votos (Tier)
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

        # --- GESTIÓN DE HISTORIAL (PAYLOADS, FLAGS Y COMENTARIOS) ---
        
        # Construimos el bloque nuevo
        voto_str = "✅ OK" if es_aprobado else "🔴 KO"
        nuevo_payload_text = f"{reviewer} ({voto_str}):\n{resumen_tesis}\n" + "\n".join(all_flags_list)
        
        new_green = f"{reviewer}: " + "\n".join([f for f in all_flags_list if "🟢" in f])
        new_red = f"{reviewer}: " + "\n".join([f for f in all_flags_list if "🔴" in f or "🟡" in f])

        def concat(nuevo, field):
            ex = entry_values.get(field, [{}])[0].get("value", "")
            return f"{nuevo}\n---\n{ex}" if ex else nuevo

        final_payload = concat(nuevo_payload_text, "signals_qualified")
        final_green = concat(new_green, "green_flags_qualified")
        final_red = concat(new_red, "red_flags_qualified")

        ex_comments = entry_values.get("signals_comments_qualified", [{}])[0].get("value", "")
        final_comments = f"{new_comment}\n---\n{ex_comments}" if (new_comment and ex_comments) else (new_comment or ex_comments)

        # Status y Tier 2
        current_st_list = entry_values.get("status", [])
        default_status = current_st_list[0].get("status", {}).get("title", "") if current_st_list else ""
        status, qualified = calculate_funnel_status(tier_actual, t1_ok, t1_ko, t2_ok, t2_ko, default_status)

        # Preparar Payload Final para Attio
        final_attio_data = {
            "signals_qualified": [{"value": final_payload}],
            "green_flags_qualified": [{"value": final_green}],
            "red_flags_qualified": [{"value": final_red}],
            "signals_comments_qualified": [{"value": final_comments}],
            "status": [{"status": status}]
        }

        # Lógica de Conflicto Tier 1
        if tier_actual == "Tier 1" and t1_ok == 1 and t1_ko == 1:
            final_attio_data["tier_5"] = [{"status": "Tier 2"}]

        # Marcar quién votó
        voter_field = ""
        if tier_actual == "Tier 1":
            voter_field = "tier_1_ok" if es_aprobado else "tier_1_ko"
        else:
            voter_field = "tier_2_ok" if es_aprobado else "tier_2_ko"
        
        final_attio_data[voter_field] = [{"option": reviewer}]

        if not qualified:
            final_attio_data["reason"] = [{"status": "Signals (Qualified)"}]

        # SUBIR TODO
        await upload_attio_entry(entry_id, final_attio_data)
        
        return {"status": "success", "es_aprobado": es_aprobado}

    except Exception as e:
        logger.error(f"Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))