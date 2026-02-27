from fastapi import FastAPI, Request, HTTPException
import httpx
import os
import logging
import uvicorn

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

# Constantes de índices (Tally)
REVIEWER_INDEX = 0
DOMAIN_INDEX = 1
FLAGS_START = 2
FLAGS_END = 9
MULTI_FLAGS_START = 9
MULTI_FLAGS_END = 11
COMMENTS_INDEX = 11

app = FastAPI()

# ---FUNCIONES AUXILIARES ORIGINALES---

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
    payload = {
        "filter": {
            "associated_company": {
                "target_object": "companies",
                "target_record_id": company_id
            }
        },
        "limit": 1
    }
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
    payload = {
        "filter": {
            "path": [[LIST_SLUG, "parent_record"], ["deals", "record_id"]],
            "constraints": {"value": deal_id}
        },
        "limit": 1
    }
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

def generar_payload(form_data):
    questions = form_data.get("submission", {}).get("questions", [])
    if len(questions) < COMMENTS_INDEX + 1:
        raise ValueError("Form data incompleto")

    reviewer = questions[REVIEWER_INDEX].get("value", "")
    domain = questions[DOMAIN_INDEX].get("value", "")
    comments_raw = questions[COMMENTS_INDEX].get("value", "")
    
    # Formateamos el comentario con el nombre del revisor
    comments = f"{reviewer}: {comments_raw}" if comments_raw else ""

    green_flags, red_flags, payload = f"{reviewer}:\n", f"{reviewer}:\n", f"{reviewer}:\n"

    all_flags_list = questions[FLAGS_START:FLAGS_END]
    for q in questions[MULTI_FLAGS_START:MULTI_FLAGS_END]:
        val = q.get("value")
        if isinstance(val, list):
            for item in val: all_flags_list.append({"value": item})

    for question in all_flags_list:
        flag = question.get("value", "")
        if not flag: continue
        payload += f"{flag}\n"
        if "🟢" in flag: green_flags += f"{flag}\n"
        elif "🔴" in flag: red_flags += f"{flag}\n"

    return domain, payload, green_flags, red_flags, comments, reviewer

def calculate_funnel_status(tier_actual, t1_ok, t1_ko, t2_ok, t2_ko, default_status=None):
    if tier_actual == "Tier 2" or (t1_ok >= 1 and t1_ko >= 1):
        if t2_ok >= 2: return "First interaction", True
        if t2_ko >= 2: return "Killed", False
        return default_status, True

    if t1_ok >= 2: return "First interaction", True
    if t1_ko >= 2: return "Killed", False

    return default_status if default_status else "Initial screening", True

# --- ACTIONS ATTIO ---

async def upload_reviewer_ko_ok(entry_id, payload_single, reviewer, tier):
    url = f"{BASE_URL}/lists/{LIST_SLUG}/entries/{entry_id}"
    num_reds = payload_single.count("🔴")
    field = ""
    if tier == "Tier 1":
        field = "tier_1_ko" if num_reds > 0 else "tier_1_ok"
    elif tier == "Tier 2":
        field = "tier_2_ko" if num_reds > 0 else "tier_2_ok"
    
    if not field: return
    data = {"data": {"entry_values": {field: [{"option": reviewer}]}}}
    async with httpx.AsyncClient(timeout=30.0) as client:
        await client.patch(url, headers=HEADERS, json=data)

async def upload_senior_needed(entry_id):
    url = f"{BASE_URL}/lists/{LIST_SLUG}/entries/{entry_id}"
    data = {"data": {"entry_values": {"tier_5": [{"status": "Tier 2"}]}}}
    async with httpx.AsyncClient(timeout=30.0) as client:
        await client.patch(url, headers=HEADERS, json=data)

async def upload_attio_entry(entry_id, payload, green, red, comments, status, qualified=True):
    url = f"{BASE_URL}/lists/{LIST_SLUG}/entries/{entry_id}"
    entry_values = {
        "signals_qualified": [{"value": payload}],
        "green_flags_qualified": [{"value": green}],
        "red_flags_qualified": [{"value": red}],
        "status": [{"status": status}]
    }
    # Solo subimos el comentario si hay contenido
    if comments and comments.strip():
        entry_values["signals_comments_qualified"] = [{"value": comments}]
        
    if not qualified:
        entry_values["reason"] = [{"status": "Signals (Qualified)"}]

    data = {"data": {"entry_values": entry_values}}
    async with httpx.AsyncClient(timeout=30.0) as client:
        res = await client.patch(url, headers=HEADERS, json=data)
        res.raise_for_status()

# --- WEBHOOK PRINCIPAL ---

@app.post("/webhook")
async def handle_signals(request: Request):
    try:
        form_data = await request.json()
        domain, payload, green_flags, red_flags, new_comment, reviewer = generar_payload(form_data)
        
        company_id = await find_company_id_from_domain(domain)
        deal_id = await find_deal_from_company_id(company_id)
        entry_id, entry_values = await find_entry_from_deal_id(deal_id)

        if not entry_id:
            raise HTTPException(status_code=404, detail="Entry no encontrada")

        tier_list = entry_values.get("tier_5", [])
        tier_actual = tier_list[0].get("status", {}).get("title", "Tier 1") if tier_list else "Tier 1"
        
        await upload_reviewer_ko_ok(entry_id, payload, reviewer, tier_actual)

        # Conteo de votos
        t1_ok = len(entry_values.get("tier_1_ok", []))
        t1_ko = len(entry_values.get("tier_1_ko", []))
        t2_ok = len(entry_values.get("tier_2_ok", []))
        t2_ko = len(entry_values.get("tier_2_ko", []))

        if tier_actual == "Tier 1":
            if payload.count("🔴") > 0: t1_ko += 1
            else: t1_ok += 1
        else:
            if payload.count("🔴") > 0: t2_ko += 1
            else: t2_ok += 1

        # --- GESTIÓN DE HISTORIAL (PAYLOAD, FLAGS Y COMENTARIOS) ---
        
        # Historial de Payloads y Flags
        ex_payload_list = entry_values.get("signals_qualified", [])
        if ex_payload_list:
            ex_p = ex_payload_list[0].get("value", "")
            ex_g = entry_values.get("green_flags_qualified", [{}])[0].get("value", "")
            ex_r = entry_values.get("red_flags_qualified", [{}])[0].get("value", "")
            payload = f"{payload}\n---\n{ex_p}"
            green_flags = f"{green_flags}\n---\n{ex_g}"
            red_flags = f"{red_flags}\n---\n{ex_r}"

        # Historial de Comentarios
        ex_comments_list = entry_values.get("signals_comments_qualified", [])
        ex_comments = ex_comments_list[0].get("value", "") if ex_comments_list else ""
        
        if new_comment:
            if ex_comments:
                final_comments = f"{new_comment}\n---\n{ex_comments}"
            else:
                final_comments = new_comment
        else:
            final_comments = ex_comments # Si no hay comentario nuevo, mantenemos el viejo

        # --- LÓGICA DE STATUS Y TIER 2 ---

        current_st_list = entry_values.get("status", [])
        default_status = current_st_list[0].get("status", {}).get("title", "") if current_st_list else ""
        status, qualified = calculate_funnel_status(tier_actual, t1_ok, t1_ko, t2_ok, t2_ko, default_status)

        if tier_actual == "Tier 1" and t1_ok == 1 and t1_ko == 1:
            await upload_senior_needed(entry_id)

        # Guardar todo en Attio (usando final_comments concatenado)
        await upload_attio_entry(entry_id, payload, green_flags, red_flags, final_comments, status, qualified)
        
        return {"status": "success", "entry_id": entry_id}

    except Exception as e:
        logger.error(f"Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))