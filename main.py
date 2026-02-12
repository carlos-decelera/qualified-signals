from fastapi import FastAPI, Request, HTTPException
import httpx
import os
import logging
import uvicorn

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
            "parent_record": deal_id
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
            entry_values = data[0].get("values", {})
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

        if flag.startswith("🟢"):
            green_flags += flag
            green_flags += "\n"
        elif flag.startswith("🔴"):
            red_flags += flag
            red_flags += "\n"  # Corregido de "red_flag"

    # Procesar flags múltiples
    for question in questions[MULTI_FLAGS_START:MULTI_FLAGS_END]:
        for flag in question.get("value", []):
            payload += flag
            payload += "\n"

            if flag.startswith("🟢"):
                green_flags += flag
                green_flags += "\n"
            elif flag.startswith("🔴"):
                red_flags += flag
                red_flags += "\n"  # Corregido de "red_flag"
    
    comments = questions[COMMENTS_INDEX].get("value", "")
    
    logger.info(f"✅ Payload generado para dominio: {domain}")
    return domain, payload, green_flags, red_flags, comments


async def upload_attio_entry(entry_id, payload, green_flags, red_flags, comments):
    """Actualiza el entry en Attio con los datos del formulario"""
    
    url = f"{BASE_URL}/lists/{LIST_SLUG}/entries/{entry_id}"
    data = {
        "data": {
            "entry_values": {
                "signals_qualified": payload,
                "green_flags_qualified": green_flags,
                "red_flags_qualified": red_flags,
                "signals_comments_qualified": comments
            }
        }
    }

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
        domain, payload, green_flags, red_flags, comments = generar_payload(form_data)
        
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

        # Concatenar con valor existente (nuevo contenido primero)
        existing_value = entry_values.get("signals_qualified", [{}])[0].get("value", "")
        if existing_value:
            payload = payload + "\n---\n" + existing_value
            
        # Actualizar entry
        result = await upload_attio_entry(entry_id, payload, green_flags, red_flags, comments)
        
        logger.info("✅ Proceso completado exitosamente")
        return {"status": "success", "message": "Signals procesados correctamente", "entry_id": entry_id}

    except HTTPException:
        raise
    except ValueError as e:
        logger.error(f"Error de validación: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error inesperado: {e}")
        raise HTTPException(status_code=500, detail="Error interno del servidor")


@app.get("/health")
async def health_check():
    """Endpoint para verificar que el servicio está funcionando"""
    return {"status": "healthy", "service": "AttioSignals"}


if __name__ == "__main__":  # Corregido de "="
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)  # Corregido de "rin"