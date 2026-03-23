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

# Constantes de índices (Tally) - Tus originales
REVIEWER_INDEX = 0
DOMAIN_INDEX = 1
FLAGS_START = 2
FLAGS_END = 9
MULTI_FLAGS_START = 9
MULTI_FLAGS_END = 11
COMMENTS_INDEX = 11

app = FastAPI()

# ---FUNCIONES AUXILIARES---

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

def generar_payload(form_data, tier_actual="Tier 1"):
    questions = form_data.get("submission", {}).get("questions", [])
    if len(questions) < COMMENTS_INDEX + 1:
        raise ValueError("Form data incompleto")

    reviewer = questions[REVIEWER_INDEX].get("value", "")
    domain = questions[DOMAIN_INDEX].get("value", "")
    comments_raw = questions[COMMENTS_INDEX].get("value", "")
    
    # --- RECONSTRUCCIÓN DE LISTA DE FLAGS ---
    all_flags_list = questions[FLAGS_START:FLAGS_END]
    for q in questions[MULTI_FLAGS_START:MULTI_FLAGS_END]:
        val = q.get("value")
        if isinstance(val, list):
            for item in val: all_flags_list.append({"value": item})

    # Extraemos solo el texto de los valores (ej: "🟢 OK")
    s_vals = [f.get("value", "") for f in all_flags_list]
    
    # --- NUEVA LÓGICA DE CRITERIO ---
    def check_criteria(vals, tier):
        if len(vals) < 7: return False
        
        # P1 (Índice 0)
        p1 = vals[0]
        # P2, P3, P4 (Índices 1, 2, 3)
        grupo_a = vals[1:4]
        # P5, P6, P7 (Índices 4, 7) - asumiendo que el orden sigue hasta el 7
        grupo_b = vals[4:7]

        if tier == "Tier 1":
            # P1: Verde
            cond1 = "🟢" in p1
            # P2-P4: Mínimo 2 verdes, 0 rojos
            cond2 = sum(1 for v in grupo_a if "🟢" in v) >= 2 and not any("🔴" in v for v in grupo_a)
            # P5-P7: Mínimo 1 verde, 0 rojos
            cond3 = sum(1 for v in grupo_b if "🟢" in v) >= 1 and not any("🔴" in v for v in grupo_b)
            return cond1 and cond2 and cond3

        elif tier == "Tier 2":
            # P1: Verde o Amarillo
            cond1 = "🟢" in p1 or "🟡" in p1
            # P2-P4: Mínimo 1 verde, 0 rojos
            cond2 = sum(1 for v in grupo_a if "🟢" in v) >= 1 and not any("🔴" in v for v in grupo_a)
            # P5-P7: Mínimo 1 verde, 0 rojos
            cond3 = sum(1 for v in grupo_b if "🟢" in v) >= 1 and not any("🔴" in v for v in grupo_b)
            return cond1 and cond2 and cond3
        
        return False

    es_voto_ok = check_criteria(s_vals, tier_actual)

    # --- CONSTRUCCIÓN DEL RESUMEN (PAYLOAD) ---
    voto_status = "✅" if es_voto_ok else "🔴"
    payload = f"Reviewer: {reviewer} ({tier_actual})\n"
    payload += f"Veredicto: {voto_status}\n"
    payload += "\n-- DETALLE --\n"

    green_flags, red_flags = f"{reviewer}:\n", f"{reviewer}:\n"
    for flag in s_vals:
        if not flag: continue
        payload += f"{flag}\n"
        if "🟢" in flag: green_flags += f"{flag}\n"
        elif "🔴" in flag: red_flags += f"{flag}\n"

    comments = f"{reviewer}: {comments_raw}" if comments_raw else ""
    
    return domain, payload, green_flags, red_flags, comments, reviewer, es_voto_ok

def calculate_funnel_status(tier_actual, t1_ok, t1_ko, t2_ok, t2_ko, default_status=None):
    if tier_actual == "Tier 2" or (t1_ok >= 1 and t1_ko >= 1):
        if t2_ok >= 2: return "First interaction", True
        if t2_ko >= 2: return "Killed", False
        return default_status, True

    if t1_ok >= 2: return "First interaction", True
    if t1_ko >= 2: return "Killed", False

    return default_status if default_status else "Initial screening", True

async def upload_reviewer_ko_ok(entry_id, es_voto_ok, reviewer, tier):
    url = f"{BASE_URL}/lists/{LIST_SLUG}/entries/{entry_id}"
    field = ""
    if tier == "Tier 1":
        field = "tier_1_ok" if es_voto_ok else "tier_1_ko"
    elif tier == "Tier 2":
        field = "tier_2_ok" if es_voto_ok else "tier_2_ko"
    
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

        questions = form_data.get("submission", {}).get("questions", [])
        domain = questions[DOMAIN_INDEX].get("value", "")
        
        company_id = await find_company_id_from_domain(domain)
        deal_id = await find_deal_from_company_id(company_id)
        entry_id, entry_values = await find_entry_from_deal_id(deal_id)

        if not entry_id:
            raise HTTPException(status_code=404, detail="Entry no encontrada")

        tier_list = entry_values.get("tier_5", [])
        tier_actual = tier_list[0].get("status", {}).get("title", "Tier 1") if tier_list else "Tier 1"

        _, payload, green_flags, red_flags, new_comment, reviewer,es_voto_ok = generar_payload(form_data, tier_actual)
        
        await upload_reviewer_ko_ok(entry_id, es_voto_ok, reviewer, tier_actual)

        t1_ok = len(entry_values.get("tier_1_ok", []))
        t1_ko = len(entry_values.get("tier_1_ko", []))
        t2_ok = len(entry_values.get("tier_2_ok", []))
        t2_ko = len(entry_values.get("tier_2_ko", []))

        if tier_actual == "Tier 1":
            if es_voto_ok: t1_ok += 1
            else: t1_ko += 1
        else:
            if es_voto_ok: t2_ok += 1
            else: t2_ko += 1

        ex_payload_list = entry_values.get("signals_qualified", [])
        if ex_payload_list:
            ex_p = ex_payload_list[0].get("value", "")
            ex_g = entry_values.get("green_flags_qualified", [{}])[0].get("value", "")
            ex_r = entry_values.get("red_flags_qualified", [{}])[0].get("value", "")
            payload = f"{payload}\n---\n{ex_p}"
            green_flags = f"{green_flags}\n---\n{ex_g}"
            red_flags = f"{red_flags}\n---\n{ex_r}"

        ex_comments_list = entry_values.get("signals_comments_qualified", [])
        ex_comments = ex_comments_list[0].get("value", "") if ex_comments_list else ""
        
        if new_comment:
            final_comments = f"{new_comment}\n---\n{ex_comments}" if ex_comments else new_comment
        else:
            final_comments = ex_comments

        current_st_list = entry_values.get("status", [])
        default_status = current_st_list[0].get("status", {}).get("title", "") if current_st_list else ""
        status, qualified = calculate_funnel_status(tier_actual, t1_ok, t1_ko, t2_ok, t2_ko, default_status)

        if tier_actual == "Tier 1" and t1_ok == 1 and t1_ko == 1:
            await upload_senior_needed(entry_id)

        await upload_attio_entry(entry_id, payload, green_flags, red_flags, final_comments, status, qualified)
        
        return {"status": "success", "veredicto": "OK" if es_voto_ok else "KO"}

    except Exception as e:
        logger.error(f"Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))