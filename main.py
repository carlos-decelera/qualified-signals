import os
import json
import logging
import httpx
from fastapi import FastAPI, Request
import uvicorn

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("AttioSignals")

app = FastAPI()

# Configuración
ATTIO_TOKEN = os.getenv("ATTIO_API_KEY")
LIST_SLUG = "menorca_2026"
BASE_URL = "https://api.attio.com/v2"

headers = {
    "Authorization": f"Bearer {ATTIO_TOKEN}",
    "Content-Type": "application/json"
}

# Mapeo por ID de Fillout (Indispensable para que no rompa si cambia el texto)
ID_MAP = {
    "1My6": "Fundadores",
    "7Z9B": "Non-obvius insight",
    "eKvZ": "Ola estructural o moda pasajera",
    "6QdT": "Dificil de replicar?",
    "c3RS": "Señales de tracción organica?",
    "qFCQ": "Podría crear o definir una categoría?",
    "vemj": "Encaja bien con Decelera?"
}

def format_evaluations(history_obj: dict) -> str:
    """
    Formats all evaluation data grouped by reviewer.
    Each reviewer shows: answers, red flags, green flags, comment.
    Clean output - no JSON, just readable text.
    """
    lines = []
    
    for evaluator, data in history_obj.items():
        lines.append(f"📋 EVALUADOR: {evaluator}")
        lines.append("")
        
        # Pillars/Answers
        pillars = data.get("pillars", {})
        for pillar, value in pillars.items():
            lines.append(f"▸ {pillar}")
            lines.append(f"   {value}")
            lines.append("")
        
        # Red Flags for this evaluator
        red_flags = data.get("red_flags", [])
        if red_flags:
            lines.append("🔴 Red Flags:")
            for flag in red_flags:
                lines.append(f"   • {flag}")
            lines.append("")
        
        # Green Flags for this evaluator
        green_flags = data.get("green_flags", [])
        if green_flags:
            lines.append("🟢 Green Flags:")
            for flag in green_flags:
                lines.append(f"   • {flag}")
            lines.append("")
        
        # Comment for this evaluator
        comment = data.get("comment", "")
        if comment:
            lines.append(f"💬 Comentario: {comment}")
            lines.append("")
        
        lines.append("")
    
    return "\n".join(lines).strip()

@app.post("/webhook")
async def handle_signals(request: Request):
    try:
        data = await request.json()
        # Mantenemos la estructura de 'body' que te funciona
        questions = data.get("submission", {}).get("questions", [])
        
        # --- IDENTIFICACIÓN POR ID ---
        # Buscamos los valores clave recorriendo la lista por ID
        domain_query = next((q.get("value") for q in questions if q.get("id") == "tNm2"), "")
        reviewer_name = next((q.get("value") for q in questions if q.get("id") == "kDiW"), "Anónimo")
        new_comment_text = next((q.get("value") for q in questions if q.get("id") == "o9wQ"), "")
        new_comment = f"{reviewer_name}: {new_comment_text}" if new_comment_text else ""

        if not domain_query:
            return {"status": "error", "message": "Domain ID (tNm2) not found in payload"}

    except Exception as e:
        return {"status": "error", "message": f"Invalid webhook structure: {str(e)}"}

    async with httpx.AsyncClient(timeout=30.0) as client:
        # 1. BUSCAR RECORD POR DOMAIN (Exactamente como lo tenías)
        search_payload = {
            "query": domain_query,
            "objects": ["companies"],
            "request_as": {"type": "workspace"},
            "limit": 1
        }
        search_res = await client.post(f"{BASE_URL}/objects/records/search", json=search_payload, headers=headers)
        search_data = search_res.json().get("data", [])
        
        if not search_data:
            return {"status": "ignored", "reason": "company_not_found"}
        
        record_id = search_data[0]["id"]["record_id"]

        # 2. BUSCAR ENTRY EN LISTA POR RECORD_ID (Exactamente como lo tenías)
        query_payload = {
            "limit": 1,
            "filter": {
                "parent_record": {
                    "target_record_id": {"$eq": record_id}
                }
            }
        }
        entry_res = await client.post(f"{BASE_URL}/lists/{LIST_SLUG}/entries/query", json=query_payload, headers=headers)
        entry_data = entry_res.json().get("data", [])

        if not entry_data:
            return {"status": "ignored", "reason": "entry_not_found"}

        entry = entry_data[0]
        entry_id = entry["id"]["entry_id"]
        existing_values = entry.get("entry_values", {})
        
        # Log existing field names to verify signals_raw_data exists
        logger.info(f"📋 Existing fields in entry: {list(existing_values.keys())}")

        # 3. LÓGICA DE PROCESAMIENTO
        clean_answers = {}
        reviewer_red_flags = []
        reviewer_green_flags = []

        # Extraer datos existentes del campo JSON separado (signals_raw_data)
        try:
            raw_data = existing_values.get("signals_raw_data", [{}])[0].get("value", "{}")
            history_obj = json.loads(raw_data) if raw_data else {}
        except: history_obj = {}

        # Procesar preguntas del Webhook por ID
        for q in questions:
            qid = q.get("id")
            value = q.get("value")

            # Mapeo de respuestas principales por ID
            if qid in ID_MAP:
                clean_val = str(value).strip().replace("\n", "")
                clean_answers[ID_MAP[qid]] = clean_val

            # Procesar Flags SOLO de las preguntas dedicadas (8-Red Flags y 9-Green Flags)
            # IDs: 9D3f = Red Flags, fSUA = Green Flags
            if qid in ["9D3f", "fSUA"] and isinstance(value, list):
                for item in value:
                    item_clean = str(item).strip().replace("\n", "")
                    if "🟢" in item_clean: reviewer_green_flags.append(item_clean)
                    if "🔴" in item_clean: reviewer_red_flags.append(item_clean)

        # 4. GUARDAR TODO AGRUPADO POR REVIEWER
        history_obj[reviewer_name] = {
            "pillars": clean_answers,
            "red_flags": reviewer_red_flags,
            "green_flags": reviewer_green_flags,
            "comment": new_comment_text  # Just the comment text, not prefixed
        }
        
        # Collect all signals and comments for the summary fields
        all_red_signals = []
        all_green_signals = []
        all_comments = []
        
        for name, data in history_obj.items():
            # Extract SIGNALS from evaluation pillars (🟢/🔴 answers)
            pillars = data.get("pillars", {})
            for pillar, value in pillars.items():
                if "🟢" in value:
                    all_green_signals.append(f"[{name}] {pillar}: {value}")
                elif "🔴" in value:
                    all_red_signals.append(f"[{name}] {pillar}: {value}")
            
            # Collect comments
            comment = data.get("comment", "")
            if comment:
                all_comments.append(f"{name}: {comment}")
        
        attio_body = {
            "data": {
                "entry_values": {
                    "signals_qualified": format_evaluations(history_obj),  # Clean formatted display
                    "red_flags_qualified": "\n".join(all_red_signals),
                    "green_flags_qualified": "\n".join(all_green_signals),
                    "signals_comments_qualified": "\n".join(all_comments)  # Changed to signals_comments
                }
            }
        }

        # Log what we're about to send
        logger.info(f"📤 Sending to Attio for entry {entry_id}")
        logger.info(f"📤 history_obj: {history_obj}")
        logger.info(f"📤 attio_body: {json.dumps(attio_body, indent=2)}")

        # 5. ACTUALIZAR ENTRY (PATCH)
        patch_res = await client.patch(
            f"{BASE_URL}/lists/{LIST_SLUG}/entries/{entry_id}",
            json=attio_body,
            headers=headers
        )

        logger.info(f"📥 Attio response status: {patch_res.status_code}")
        logger.info(f"📥 Attio response body: {patch_res.text}")

        if patch_res.status_code == 200:
            logger.info(f"✅ Entry {entry_id} actualizada correctamente")
            return {"status": "success", "entry_id": entry_id, "data_saved": history_obj}
        else:
            logger.error(f"❌ Error al actualizar: {patch_res.text}")
            return {"status": "error", "detail": patch_res.text, "attio_body_sent": attio_body}

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)