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
    
    # Extraemos las 7 preguntas base (P1 a P7)
    # P1: Thesis | P2-P4: Críticos | P5-P7: Complementarios
    base_flags = [q.get("value", "") for q in questions[FLAGS_START:FLAGS_END]]
    
    # Extraemos multi-flags (P8+) para el detalle visual
    multi_flags = []
    for q in questions[MULTI_FLAGS_START:MULTI_FLAGS_END]:
        val = q.get("value")
        if isinstance(val, list): multi_flags.extend(val)
        elif val: multi_flags.append(val)

    def evaluar_veredicto(p_list):
        if len(p_list) < 7: 
            logger.warning(f"Lista de preguntas corta: {len(p_list)}")
            return "⚠️ ERROR: Faltan preguntas", False
        
        p1 = p_list[0]          # Thesis
        criticos = p_list[1:4]  # P2, P3, P4
        compl = p_list[4:7]     # P5, P6, P7

        # Conteos precisos
        c_verdes = sum(1 for v in criticos if "🟢" in v)
        c_rojos = sum(1 for v in criticos if "🔴" in v)
        comp_verdes = sum(1 for v in compl if "🟢" in v)
        comp_rojos = sum(1 for v in compl if "🔴" in v)
        
        # Log para debug (ver esto en la consola)
        logger.info(f"DEBUG EVAL: P1:{p1} | Crit_V:{c_verdes} Crit_R:{c_rojos} | Comp_V:{comp_verdes} Comp_R:{comp_rojos}")

        # 1. 🔥 STRONG YES
        if "🟢" in p1 and c_verdes >= 2 and comp_verdes >= 1 and c_rojos == 0 and comp_rojos == 0:
            return "🔥 STRONG YES (Pre-IC)", True

        # 2. 🤢 WEAK YES (Tu caso: P1 Amarillo + 1 Verde Crit + 1 Verde Compl + 0 Rojas)
        # Ajustado: Ahora solo pide c_verdes >= 1 y comp_verdes >= 1
        if ("🟢" in p1 or "🟡" in p1) and c_verdes >= 1 and comp_verdes >= 1 and c_rojos == 0 and comp_rojos == 0:
            return "🤢 WEAK YES (Deep Dive)", True

        # 3. 🤔 WEAK NO
        # Si hay CUALQUIER rojo en complementarios, o si no llegamos al mínimo de verdes
        if ("🟢" in p1 or "🟡" in p1) and (comp_rojos >= 1 or (c_verdes == 0 and comp_verdes == 0)):
            return "🤔 WEAK NO (Descarte)", False

        # 4. 🛑 STRONG NO
        if "🔴" in p1 or c_rojos >= 1:
            return "🛑 STRONG NO (Muerte)", False

        return "❓ INDEFINIDO (KO por defecto)", False

    veredicto_nombre, es_voto_ok = evaluar_veredicto(base_flags)

    # --- CONSTRUCCIÓN DEL PAYLOAD ---
    voto_icon = "✅" if es_voto_ok else "❌"
    payload = f"Reviewer: {reviewer} ({tier_actual})\n"
    payload += f"Veredicto: {voto_icon} {veredicto_nombre}\n"
    payload += "\n-- DETALLE --\n"

    all_flags = base_flags + multi_flags
    green_txt, red_txt = f"{reviewer}:\n", f"{reviewer}:\n"
    
    for flag in all_flags:
        if not flag: continue
        payload += f"{flag}\n"
        if "🟢" in flag: green_txt += f"{flag}\n"
        elif "🔴" in flag: red_txt += f"{flag}\n"

    comments = f"{reviewer}: {comments_raw}" if comments_raw else ""
    
    return domain, payload, green_txt, red_txt, comments, reviewer, veredicto_nombre, es_voto_ok

def calculate_funnel_status(tier_actual, status_actual, t1_ok, t1_ko, t2_ok, t2_ko, default_status=None):
    if tier_actual == "Tier 2" or (t1_ok >= 1 and t1_ko >= 1):
        if t2_ok >= 1: return "First interaction" if status_actual == "Initial screening" else "Deep dive", True
        if t2_ko >= 1: return "Killed", False
        return default_status, True

    if t1_ok >= 1: return "First interaction" if status_actual == "Initial screening" else "Deep dive", True
    if t1_ko >= 1: return "Killed", False

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

async def upload_attio_entry(entry_id, payload, green, red, comments, status, veredicto_nombre, qualified=True):
    url = f"{BASE_URL}/lists/{LIST_SLUG}/entries/{entry_id}"
    entry_values = {
        "signals_qualified": [{"value": payload}],
        "green_flags_qualified": [{"value": green}],
        "red_flags_qualified": [{"value": red}],
        "status": [{"status": status}],
        "screening_conviction": [{"value": veredicto_nombre}]
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
        status_list = entry_values.get("status", [])
        status_actual = status_list[0].get("status", {}).get("title") if status_list else "" 

        _, payload, green_flags, red_flags, new_comment, reviewer, veredicto_nombre, es_voto_ok = generar_payload(form_data, tier_actual)
        
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
        status, qualified = calculate_funnel_status(tier_actual, status_actual, t1_ok, t1_ko, t2_ok, t2_ko, default_status)

        if tier_actual == "Tier 1" and t1_ok == 1 and t1_ko == 1:
            await upload_senior_needed(entry_id)

        new_conviction_line = f"{reviewer}: {veredicto_nombre}"
        ex_conviction_list = entry_values.get("screening_conviction", [])
        ex_conviction = ex_conviction_list[0].get("value", "") if ex_conviction_list else ""

        final_conviction = f"{new_conviction_line}\n---\n{ex_conviction}" if ex_conviction else new_conviction_line

        await upload_attio_entry(entry_id, payload, green_flags, red_flags, final_comments, status, final_conviction, qualified)
        
        return {"status": "success", "veredicto": "OK" if es_voto_ok else "KO"}

    except Exception as e:
        logger.error(f"Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))