import os
import logging
import httpx
import uvicorn
from fastapi import FastAPI, Request, HTTPException

# --- CONFIGURACIÓN DE LOGS ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("DeceleraSignals")

# --- CONFIGURACIÓN ATTIO ---
ATTIO_API_KEY = os.getenv("ATTIO_API_KEY")
LIST_SLUG = os.getenv("LIST_SLUG")
BASE_URL = "https://api.attio.com/v2"
HEADERS = {
    "Authorization": f"Bearer {ATTIO_API_KEY}",
    "Content-Type": "application/json"
}

# --- ÍNDICES DE TALLY (Ajusta según tu formulario) ---
# Estos índices representan la posición de la pregunta en el JSON de Tally (empezando en 0)
REVIEWER_INDEX = 0
DOMAIN_INDEX = 1
FLAGS_START = 2   # Aquí empieza S1 (Founder)
FLAGS_END = 9     # Aquí termina S7 (Fit Thesis)
COMMENTS_INDEX = 11

app = FastAPI()

# --- LÓGICA DE NEGOCIO (IMAGEN) ---

def validar_tesis_decelera(signals_list):
    """
    Aplica las reglas de la imagen:
    - Gatekeepers (S1, S2, S7): Necesitan 3 verdes. Amarillo o Rojo = KO.
    - Compensadores (S3, S4, S5, S6): Necesitan al menos 2 verdes.
    """
    if len(signals_list) < 7:
        return False, "Error: Faltan señales en el formulario"

    # Mapeo según el orden de la tesis
    s1, s2, s3, s4, s5, s6, s7 = signals_list

    # 1. Lógica Gatekeepers (S1, S2, S7)
    gatekeepers = [s1, s2, s7]
    # Si alguno NO es verde (es decir, es rojo o amarillo), es KO.
    gk_ok = all("🟢" in g for g in gatekeepers)
    
    # 2. Lógica Compensadores (S3, S4, S5, S6)
    compensadores = [s3, s4, s5, s6]
    comp_greens = sum(1 for c in compensadores if "🟢" in c)
    comp_ok = comp_greens >= 2

    resultado_final = gk_ok and comp_ok

    # Construcción del reporte para Attio
    detalle = (
        f"Gatekeepers (S1,S2,S7): {'✅ PASA' if gk_ok else '❌ KO (Veto)'}\n"
        f"Compensadores (Min. 2): {'✅ PASA' if comp_ok else '❌ KO'} ({comp_greens}/4 verdes)\n"
        f"Signals: S1:{s1} S2:{s2} S7:{s7} | S3:{s3} S4:{s4} S5:{s5} S6:{s6}"
    )

    return resultado_final, detalle

# --- FUNCIONES AUXILIARES DE ATTIO ---

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

def calculate_funnel_status(tier_actual, t1_ok, t1_ko, t2_ok, t2_ko, default_status=None):
    # Si ya estamos en Tier 2 o hay conflicto en Tier 1
    if tier_actual == "Tier 2" or (t1_ok >= 1 and t1_ko >= 1):
        if t2_ok >= 2: return "First interaction", True
        if t2_ko >= 2: return "Killed", False
        return default_status, True

    # Lógica estándar de Tier 1
    if t1_ok >= 2: return "First interaction", True
    if t1_ko >= 2: return "Killed", False

    return default_status if default_status else "Initial screening", True

async def upload_attio_full(entry_id, payload_data):
    url = f"{BASE_URL}/lists/{LIST_SLUG}/entries/{entry_id}"
    data = {"data": {"entry_values": payload_data}}
    async with httpx.AsyncClient(timeout=30.0) as client:
        res = await client.patch(url, headers=HEADERS, json=data)
        res.raise_for_status()

# --- WEBHOOK PRINCIPAL ---

@app.post("/webhook")
async def handle_signals(request: Request):
    try:
        form_data = await request.json()
        questions = form_data.get("submission", {}).get("questions", [])

        # 1. Extraer datos básicos
        reviewer = questions[REVIEWER_INDEX].get("value", "Desconocido")
        domain = questions[DOMAIN_INDEX].get("value", "")
        new_comment_raw = questions[COMMENTS_INDEX].get("value", "")
        
        # 2. Extraer señales y validar
        signals_values = [q.get("value", "") for q in questions[FLAGS_START:FLAGS_END]]
        es_aprobado, resumen_validación = validar_tesis_decelera(signals_values)

        # 3. Buscar en Attio
        company_id = await find_company_id_from_domain(domain)
        deal_id = await find_deal_from_company_id(company_id)
        entry_id, entry_values = await find_entry_from_deal_id(deal_id)

        if not entry_id:
            raise HTTPException(status_code=404, detail="Entry no encontrada en Attio")

        # 4. Determinar Tier y Conteo de Votos
        tier_list = entry_values.get("tier_5", [])
        tier_actual = tier_list[0].get("status", {}).get("title", "Tier 1") if tier_list else "Tier 1"
        
        t1_ok = len(entry_values.get("tier_1_ok", []))
        t1_ko = len(entry_values.get("tier_1_ko", []))
        t2_ok = len(entry_values.get("tier_2_ok", []))
        t2_ko = len(entry_values.get("tier_2_ko", []))

        # Sumar el voto actual al conteo para el cálculo de status
        if tier_actual == "Tier 1":
            if es_aprobado: t1_ok += 1
            else: t1_ko += 1
        else:
            if es_aprobado: t2_ok += 1
            else: t2_ko += 1

        # 5. Gestión de Historial (Lo nuevo arriba)
        def get_old_val(field):
            return entry_values.get(field, [{}])[0].get("value", "")

        # Payload de Señales estructurado
        nuevo_bloque_signals = f"**Analista: {reviewer}**\nVeredicto: {'✅ PASA' if es_aprobado else '🔴 KO'}\n{resumen_validación}"
        old_signals = get_old_val("signals_qualified")
        final_signals = f"{nuevo_bloque_signals}\n---\n{old_signals}" if old_signals else nuevo_bloque_signals

        # Green y Red Flags (resumen rápido de iconos)
        green_str = f"{reviewer}: " + " ".join([v for v in signals_values if "🟢" in v])
        old_green = get_old_val("green_flags_qualified")
        final_green = f"{green_str}\n---\n{old_green}" if old_green else green_str

        red_str = f"{reviewer}: " + " ".join([v for v in signals_values if "🔴" in v or "🟡" in v])
        old_red = get_old_val("red_flags_qualified")
        final_red = f"{red_str}\n---\n{old_red}" if old_red else red_str

        # Comentarios
        old_comm = get_old_val("signals_comments_qualified")
        if new_comment_raw:
            new_comm_fmt = f"{reviewer}: {new_comment_raw}"
            final_comm = f"{new_comm_fmt}\n---\n{old_comm}" if old_comm else new_comm_fmt
        else:
            final_comm = old_comm

        # 6. Cálculo de Status Final
        current_st_list = entry_values.get("status", [])
        default_status = current_st_list[0].get("status", {}).get("title", "") if current_st_list else ""
        status_final, qualified = calculate_funnel_status(tier_actual, t1_ok, t1_ko, t2_ok, t2_ko, default_status)

        # 7. Preparar Payload Final para Attio
        attio_payload = {
            "signals_qualified": [{"value": final_signals}],
            "green_flags_qualified": [{"value": final_green}],
            "red_flags_qualified": [{"value": final_red}],
            "signals_comments_qualified": [{"value": final_comm}],
            "status": [{"status": status_final}]
        }

        # Si el voto causa un conflicto (1 OK vs 1 KO en Tier 1), mover a Tier 2
        if tier_actual == "Tier 1" and t1_ok == 1 and t1_ko == 1:
            attio_payload["tier_5"] = [{"status": "Tier 2"}]

        # Guardar quién votó (Select dinámico en Attio)
        voter_field = ""
        if tier_actual == "Tier 1":
            voter_field = "tier_1_ok" if es_aprobado else "tier_1_ko"
        else:
            voter_field = "tier_2_ok" if es_aprobado else "tier_2_ko"
        
        attio_payload[voter_field] = [{"option": reviewer}]

        # Si no está calificado, marcar razón
        if not qualified:
            attio_payload["reason"] = [{"status": "Signals (Qualified)"}]

        # 8. Subida final
        await upload_attio_full(entry_id, attio_payload)

        return {"status": "success", "reviewer": reviewer, "voto": "PASA" if es_aprobado else "KO"}

    except Exception as e:
        logger.error(f"Error crítico: {e}")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)