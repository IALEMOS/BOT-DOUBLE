# collect.py
# Bot de coleta para sinais do Telegram (Double/Blaze)
# - pronto para Render (Background Worker)
# - usa StringSession via variável de ambiente (fallback para arquivo se quiser)
# - persiste WIN/LOSS no Supabase
# - fuso horário: America/Rio_Branco

import os
import re
import json
import requests
import pytz
from datetime import datetime
from telethon import TelegramClient, events
from telethon.sessions import StringSession

# ==========================
# CONFIG (tudo por variáveis de ambiente no Render)
# ==========================
API_ID  = int(os.getenv("TG_API_ID", "29224821"))  # ou defina no painel
API_HASH = os.getenv("TG_API_HASH", "e02231dc424a5e8becd21762d185e3a9")

# StringSession (recomendado no Render). Gere localmente e cole no painel.
TG_STRING_SESSION = os.getenv("TG_STRING_SESSION", "").strip()

# Fallback local (apenas se for rodar no PC com arquivo .session)
SESSION_FILE = os.getenv("TG_SESSION", "BOT_DOUBLE")

# ID numérico do chat/grupo OU @username (ex.: "@tipminer_bet_bot")
TARGET = os.getenv("TG_TARGET", "@tipminer_bet_bot")

# Supabase
SUPABASE_URL = os.getenv("SUPABASE_URL", "https://dudpqqsguexasxbrvykn.supabase.co")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "SUA_CHAVE_AQUI")
SUPABASE_TABLE = f"{SUPABASE_URL}/rest/v1/padroes"
SUPABASE_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=minimal",
}

# ==========================
# PADRÕES (ids fixos)
# ==========================
REG_PATTERNS = {
    1: "SURF - P4",
    2: "SURF - P5",
    3: "P2",
    4: "P3",
    5: "P1",
}

def _rx(name: str):
    esc = re.escape(name)
    esc = re.sub(r"\\\s\+", r"\\s+", esc)   # tolera múltiplos espaços
    esc = esc.replace(r"\ ", r"\s*")        # espaço opcional
    return re.compile(rf"\b{esc}\b", re.IGNORECASE)

RX_PATTERNS = [(pid, name, _rx(name)) for pid, name in REG_PATTERNS.items()]

# ==========================
# REGEX AUXILIARES
# ==========================
RE_HEADER_HINT = re.compile(r"POSS[ÍI]VEL\s+PADR[ÃA]O\s+IDENTIFICADO", re.I)
RE_WIN   = re.compile(r"\bWIN+\b", re.I)
RE_LOSS  = re.compile(r"\bLOSS+\b", re.I)
RE_ABORT = re.compile(r"\bABORTAD[OA]\b", re.I)
RE_GALE  = re.compile(r"GALE\s*(\d+)", re.I)

# ==========================
# ESTADO
# ==========================
threads = {}  # root_id -> {id_padrao, nome_padrao, gales, status, mensagens, ...}

# fuso do Acre
acre_tz = pytz.timezone("America/Rio_Branco")

def iso_now():
    """Retorna hora atual no fuso do Acre (ISO-8601 com offset)."""
    return datetime.now(acre_tz).isoformat()

def find_pattern_in(texto: str):
    """Retorna (id_padrao, nome_padrao) se achar na lista; senão (None, None)."""
    if not texto:
        return (None, None)
    for pid, name, rx in RX_PATTERNS:
        if rx.search(texto):
            return (pid, name)
    return (None, None)

def terminal_event(texto: str):
    if RE_ABORT.search(texto): return "aborted"
    if RE_WIN.search(texto):   return "win"
    if RE_LOSS.search(texto):  return "loss"
    return None

# ==========================
# SUPABASE: persistência
# ==========================
def save_to_supabase(payload: dict):
    """
    Insere 1 linha na tabela 'padroes' via REST.
    payload: {id_padrao, nome_padrao, hora, win, loss, gale?, color?, root_id?}
    """
    body = {
        "pattern_id": payload["id_padrao"],
        "pattern_name": payload["nome_padrao"],
        "ts": payload["hora"],           # já no fuso do Acre (string ISO-8601)
        "win": payload["win"],
        "loss": payload["loss"],
        # opcionais — habilite quando tiver esses campos
        # "gale": payload.get("gale", 0),
        # "color": payload.get("color"),
        # "root_id": payload.get("root_id"),
    }
    try:
        resp = requests.post(SUPABASE_TABLE, headers=SUPABASE_HEADERS, json=body, timeout=10)
        if resp.status_code >= 300:
            print(f"[SUPABASE] Falha {resp.status_code}: {resp.text}")
        else:
            print("[SUPABASE] Inserido com sucesso.")
    except Exception as e:
        print(f"[SUPABASE] Erro: {e}")

# ==========================
# TELEGRAM CLIENT
# ==========================
if TG_STRING_SESSION:
    client = TelegramClient(StringSession(TG_STRING_SESSION), API_ID, API_HASH)
else:
    # fallback local (arquivo). No Render, PREFIRA TG_STRING_SESSION.
    client = TelegramClient(SESSION_FILE, API_ID, API_HASH)

@client.on(events.NewMessage(chats=TARGET))
async def on_msg(event):
    msg   = event.message
    texto = msg.message or ""
    mid   = msg.id
    rid   = msg.reply_to_msg_id

    # ===== 1) Mensagem raiz? =====
    # Heurística: contém “possível padrão identificado” OU já cita um nome conhecido
    root_like = (RE_HEADER_HINT.search(texto) is not None) or (find_pattern_in(texto)[0] is not None)

    if root_like and not rid:
        id_padrao, nome_padrao = find_pattern_in(texto)
        if id_padrao is None:
            print(f"[ROOT? {mid}] Parece raiz mas não bate com a lista: {texto[:90]!r}")
            return

        threads[mid] = {
            "id_padrao": id_padrao,
            "nome_padrao": nome_padrao,
            "created_at": iso_now(),
            "gales": 0,
            "status": None,
            "mensagens": [{"id": mid, "texto": texto, "at": iso_now()}],
        }
        print("\n" + "="*60)
        print(f"[ROOT {mid}] padrão='{nome_padrao}' (id={id_padrao})")
        print("="*60)
        return

    # ===== 2) Resposta (thread) =====
    if rid:
        root_id = rid
        if root_id not in threads:
            threads[root_id] = {
                "id_padrao": None,
                "nome_padrao": None,
                "created_at": iso_now(),
                "gales": 0,
                "status": None,
                "mensagens": [],
            }

        t = threads[root_id]
        t["mensagens"].append({"id": mid, "texto": texto, "at": iso_now()})
        print(f"[RESPOSTA -> {root_id}] {texto}")

        # backfill do nome/id a partir da raiz real
        if t["id_padrao"] is None:
            try:
                root_msg = await event.get_reply_message()
                if root_msg:
                    rtext = root_msg.message or ""
                    pid, pname = find_pattern_in(rtext)
                    if pid is not None:
                        t["id_padrao"], t["nome_padrao"] = pid, pname
                        if not any(m["id"] == root_msg.id for m in t["mensagens"]):
                            t["mensagens"].insert(0, {"id": root_msg.id, "texto": rtext, "at": iso_now()})
                        print(f"   ↳ backfill padrão='{pname}' (id={pid})")
            except Exception as e:
                print(f"   ↳ backfill falhou: {e}")

        # GALE
        g = RE_GALE.search(texto)
        if g:
            try:
                t["gales"] = max(t["gales"], int(g.group(1)))
                print(f"   ↳ gale={t['gales']}")
            except:
                pass

        # Desfecho (aborted / win / loss)
        end = terminal_event(texto)
        if end:
            t["status"] = end

            if end == "aborted":
                print(f"[CLOSE {root_id}] ❌ abortado (não salva)\n")
                del threads[root_id]
                return

            if t["id_padrao"] is None:
                print(f"[CLOSE {root_id}] ⚠️ sem padrão reconhecido — não salva.\n")
                del threads[root_id]
                return

            payload = {
                "id_padrao": t["id_padrao"],
                "nome_padrao": t["nome_padrao"],
                "hora": iso_now(),                          # fuso Acre
                "win":  1 if end == "win"  else 0,
                "loss": 1 if end == "loss" else 0,
                # "gale": t["gales"],                       # habilite se quiser gravar
                # "root_id": root_id,
            }

            print(f"[CLOSE {root_id}] ✅ pronto para o banco:")
            print(json.dumps(payload, indent=2, ensure_ascii=False))
            save_to_supabase(payload)

            del threads[root_id]
            return

# ==========================
# MAIN
# ==========================
print("Ouvindo mensagens... Ctrl+C para sair.")
client.start()
client.run_until_disconnected()
