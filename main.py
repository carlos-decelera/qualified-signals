from fastapi import FastAPI, Request, HTTPException
import httpx
import os
import logging
import uvicorn
import random

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("AttioSignals")

# FLUJO DE LOS SIGNALS QUALIFIED

# ---CONFIGURACION---
ATTIO_API_KEY = os.getenv("ATTIO_API_KEY")
LIST_SLUG = os.getenv("LIST_SLUG")
BASE_URL = "https://api.attio.com/v2"
HEADERS = {
    "Authorization": f"Bearer {ATTIO_API_KEY}",
    "Content-Type": "application/json"
}

# Constantes para los índices de preguntas
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
    """Usamos el dominio para sacar el record id de la compañía"""
    
    url = f"{BASE_URL}/objects/companies/records/query"
    payload = {
        "filter": {"domains": {"domain": domain}},
        "limit": 1
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            res = await client.post(url, headers=HEADERS, json=payload)
            res.raise_for_status()
            data = res.json().get("data", [])
            
            if not data:
                logger.warning(f"No se encontró compañía con dominio: {domain}")
                return ""
            
            company_id = data[0].get("id", {}).get("record_id", "")
            logger.info(f"✅ Compañía encontrada: {company_id}")
            return company_id

        except Exception as e:
            logger.error(f"Error buscando compañía: {e}")
            return ""


async def find_deal_from_company_id(company_id: str) -> str:
    """Usamos el id de la compañia para sacar el deal asociado"""
    
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
            
            if not data:
                logger.warning(f"No se encontró deal para compañía: {company_id}")
                return ""

            deal_id = data[0].get("id", {}).get("record_id", "")
            logger.info(f"✅ Deal encontrado: {deal_id}")
            return deal_id
        
        except Exception as e:
            logger.error(f"Error buscando deal: {e}")
            return ""


async def find_entry_from_deal_id(deal_id: str):
    """Usamos el deal id para sacar el miembro de la lista que es su hijo"""
    
    url = f"{BASE_URL}/lists/{LIST_SLUG}/entries/query"
    payload = {
        "filter": {
            "path": [
                [LIST_SLUG, "parent_record"],
                ["deals", "record_id"]
            ],
            "constraints": {
                "value": deal_id
            }
        },
        "limit": 1
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            res = await client.post(url, headers=HEADERS, json=payload)
            res.raise_for_status()
            data = res.json().get("data", [])
            
            if not data:
                logger.warning(f"No se encontró entry para deal: {deal_id}")
                return "", {}

            entry_id = data[0].get("id", {}).get("entry_id", "")
            entry_values = data[0].get("entry_values", {})
            logger.info(f"✅ Entry encontrado: {entry_id}")
            
            return entry_id, entry_values

        except Exception as e:
            logger.error(f"Error buscando entry: {e}")
            return "", {}

def generar_payload(form_data):
    """Vamos a sacar unos campos bonitos del form: Resumen, Greens, Reds y Comments"""
    
    questions = form_data.get("submission", {}).get("questions", [])
    
    if len(questions) < COMMENTS_INDEX + 1:
        logger.error("Form data incompleto")
        raise ValueError("Form data no tiene suficientes preguntas")

    reviewer = questions[REVIEWER_INDEX].get("value", "")
    domain = questions[DOMAIN_INDEX].get("value", "")

    green_flags = f"{reviewer}:\n"
    red_flags = f"{reviewer}:\n"  # Inicializar con el nombre del reviewer también
    payload = f"{reviewer}:\n"

    # Procesar flags individuales
    for question in questions[FLAGS_START:FLAGS_END]:
        flag = question.get("value", "")
        payload += flag
        payload += "\n"

        if "🟢" in flag:
            green_flags += flag
            green_flags += "\n"
        elif "🔴" in flag:
            red_flags += flag
            red_flags += "\n"  # Corregido de "red_flag"

    # Procesar flags múltiples
    for question in questions[MULTI_FLAGS_START:MULTI_FLAGS_END]:
        value = question.get("value")
        if isinstance(value, list):
            for flag in value:
                if not flag: continue

                payload += flag
                payload += "\n"

                if "🟢" in flag:
                    green_flags += flag
                    green_flags += "\n"
                elif "🔴" in flag:
                    red_flags += flag
                    red_flags += "\n"  # Corregido de "red_flag"
    
    comments = questions[COMMENTS_INDEX].get("value", "")
    
    logger.info(f"✅ Payload generado para dominio: {domain}")
    return domain, payload, green_flags, red_flags, comments, reviewer

def calculate_funnel_status(payload, default_status=None):
    """Calculamos las condiciones del funnel
    -> Usar después de tener el payload completo (con lo antigup y lo nuevo)"""

    num_evaluaciones = payload.count("---") + 1
    num_green_flags = payload.count("🟢")
    num_red_flags = payload.count("🔴")

    logger.info(f"Análisis para el funnel: Evals: {num_evaluaciones}, Greens: {num_green_flags}, Reds: {num_red_flags}")

    if num_evaluaciones >= 2 and num_green_flags >= 2 and num_red_flags == 0:
        return "First interaction", True
    
    elif num_evaluaciones < 2:
        if default_status == "Initial screening":
            return "Initial screening", True
        else:
            return "First interaction", True

    elif num_evaluaciones >= 2 and num_red_flags > 0:
        return "Killed", False

    return "Killed", True

async def upload_reviewer_ko_ok(entry_id, payload_single, reviewer, tier):
    """Actualizamos los campos de vaidator"""
    url = f"{BASE_URL}/lists/{LIST_SLUG}/entries/{entry_id}"
    data = {
        "data": {
            "entry_values": {
            }
        }
    }

    num_red_flags = payload_single.count("🔴")

    if tier == "Tier 1":
        if num_red_flags > 0:
            data["data"]["entry_values"]["tier_1_ko"] = [{"option": reviewer}]
        else:
            data["data"]["entry_values"]["tier_1_ok"] = [{"option": reviewer}]
    elif tier == "Tier 2":
        if num_red_flags > 0:
            data["data"]["entry_values"]["tier_2_ko"] = [{"option": reviewer}]
        else:
            data["data"]["entry_values"]["tier_2_ok"] = [{"option": reviewer}]

    async with httpx.AsyncClient(timeout=30.0) as client:  # Agregado async y timeout
        try:
            res = await client.patch(url, headers=HEADERS, json=data)  # Agregado await
            res.raise_for_status()

            logger.info(f"✅ Entry {entry_id} actualizada validator correctamente")
            return {"status": "success", "entry_id": entry_id}
            
        except Exception as e:
            logger.error(f"❌ Error al actualizar reviewer: {e}")
            raise

async def upload_senior_needed(entry_id, total_ko, total_ok):
    """
    Si hay conflicto entre revisores (al menos un KO y al menos un OK),
    asigna un Senior al azar de la lista.
    """
    # Verificamos si hay conflicto
    hay_conflicto = len(total_ko) > 0 and len(total_ok) > 0
    
    if not hay_conflicto:
        return {"status": "skipped", "message": "No hay conflicto de opiniones"}

    url = f"{BASE_URL}/lists/{LIST_SLUG}/entries/{entry_id}"
    
    # Payload para asignar al Senior
    data = {
        "data": {
            "entry_values": {
                "tier_5": [{"option": "Tier 2"}]
            }
        }
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            res = await client.patch(url, headers=HEADERS, json=data)
            res.raise_for_status()
            logger.info(f"✅ Senior asignado a la entry {entry_id} por conflicto")
            return {"status": "success", "assigned": True}
        except Exception as e:
            logger.error(f"❌ Error al asignar senior: {e}")
            raise


async def upload_attio_entry(entry_id, payload, green_flags, red_flags, comments, status, qualified=True):
    """Actualiza el entry en Attio con los datos del formulario"""
    
    url = f"{BASE_URL}/lists/{LIST_SLUG}/entries/{entry_id}"
    data = {
        "data": {
            "entry_values": {
                "signals_qualified": [{"value": payload}],
                "green_flags_qualified": [{"value": green_flags}],
                "red_flags_qualified": [{"value": red_flags}],
                "status": [{"status": status}]
            }
        }
    }

    if comments and comments.strip():
        data["data"]["entry_values"]["signals_comments_qualified"] = [{"value": comments}]

    if not qualified:
        data["data"]["entry_values"]["reason"] = [{"status": "Signals (Qualified)"}]

    async with httpx.AsyncClient(timeout=30.0) as client:  # Agregado async y timeout
        try:
            res = await client.patch(url, headers=HEADERS, json=data)  # Agregado await
            res.raise_for_status()

            logger.info(f"✅ Entry {entry_id} actualizada correctamente")
            return {"status": "success", "entry_id": entry_id}
            
        except Exception as e:
            logger.error(f"❌ Error al actualizar entry: {e}")
            raise


# ENDPOINT PRINCIPAL
@app.post("/webhook")
async def handle_signals(request: Request):
    """Procesa el webhook de Tally con los signals qualified"""
    
    try:
        form_data = await request.json()
        logger.info("📥 Webhook recibido")

        # Generar payload del formulario
        domain, payload, green_flags, red_flags, comments, reviewer = generar_payload(form_data)
        
        if not domain:
            raise HTTPException(status_code=400, detail="Dominio no encontrado en el formulario")

        # Buscar company
        company_id = await find_company_id_from_domain(domain)
        if not company_id:
            raise HTTPException(status_code=404, detail=f"No se encontró compañía con dominio: {domain}")

        # Buscar deal
        deal_id = await find_deal_from_company_id(company_id)
        if not deal_id:
            raise HTTPException(status_code=404, detail=f"No se encontró deal para la compañía: {company_id}")

        # Buscar entry
        entry_id, entry_values = await find_entry_from_deal_id(deal_id)
        if not entry_id:
            raise HTTPException(status_code=404, detail=f"No se encontró entry para el deal: {deal_id}")

        #Ponemos el validator
        tier_list = entry_values.get("tier_5", [])
        tier = tier_list[0].get("status", {}).get("title", "") if tier_list else ""
        response = await upload_reviewer_ko_ok(entry_id, payload, reviewer, tier)

        num_red_flags_actual = payload.count("🔴")
        actual_ko = [reviewer] if num_red_flags_actual > 0 else []
        actual_ok = [reviewer] if num_red_flags_actual == 0 else []

        # 2. Sacamos los que ya estaban en Attio (foto antigua)
        tier1_ko_previo = [item.get("option", {}).get("title", "") for item in entry_values.get("tier_1_ko", [])]
        tier1_ok_previo = [item.get("option", {}).get("title", "") for item in entry_values.get("tier_1_ok", [])]

        # 3. Sumamos ambos para tener la foto real del presente
        total_ko = tier1_ko_previo + actual_ko
        total_ok = tier1_ok_previo + actual_ok

        # 4. Ahora sí, decidimos si hace falta un Senior
        # Pasamos las listas completas para que la función decida
        await upload_senior_needed(entry_id, total_ko, total_ok)

        # Concatenar con valor existente (nuevo contenido primero)
        existing_list = entry_values.get("signals_qualified", [])
        existing_value = existing_list[0].get("value", "") if existing_list else ""
        logger.info(f"existing_value: '{existing_value}'")
        logger.info(f"signals_qualified raw: {entry_values.get('signals_qualified')}")
        logger.info(f"payload final: '{payload}'")
        if existing_value:
            existing_greens_list = entry_values.get("green_flags_qualified", [])
            existing_greens = existing_greens_list[0].get("value", "") if existing_greens_list else ""

            existing_reds_list = entry_values.get("red_flags_qualified", [])
            existing_reds = existing_reds_list[0].get("value", "") if existing_reds_list else ""

            green_flags = green_flags + "\n---\n" + existing_greens
            red_flags = red_flags + "\n---\n" + existing_reds
            payload = payload + "\n---\n" + existing_value
        
        default_status_list = entry_values.get("status", [])
        default_status = default_status_list[0].get("status", "") if default_status_list else ""
        # Vamos a ver el funnel como va
        status, qualified = calculate_funnel_status(payload, default_status)

        # Actualizar entry
        result = await upload_attio_entry(entry_id, payload, green_flags, red_flags, comments, status, qualified)
        
        logger.info("✅ Proceso completado exitosamente")
        return {"status": "success", "message": "Signals procesados correctamente", "entry_id": entry_id}

    except HTTPException:
        raise
    except ValueError as e:
        logger.error(f"Error de validación: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        if hasattr(e, 'response'):
            logger.error(f"Detalle del error API: {e.response.text}") # Esto te dirá qué campo falla
        logger.error(f"Error inesperado: {e}")


@app.get("/health")
async def health_check():
    """Endpoint para verificar que el servicio está funcionando"""
    return {"status": "healthy", "service": "AttioSignals"}


if __name__ == "__main__":  # Corregido de "="
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)  # Corregido de "rin"