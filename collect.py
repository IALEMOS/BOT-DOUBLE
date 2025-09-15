from telethon import TelegramClient, events
import re, json
from datetime import datetime, timezone
import os, requests
from datetime import datetime
import pytz


API_ID = 29224821
API_HASH = "e02231dc424a5e8becd21762d185e3a9"
SESSION = "BOT DOUBLE"
TARGET = 4987907730

client = TelegramClient(SESSION, API_ID, API_HASH)

# ============== REGISTRO DE PADRÕES (id fixo) ==============
# ajuste aqui conforme quiser
REG_PATTERNS = {
    1: "P1",
    2: "SURF - P5",
    3: "P2",
}


SUPABASE_URL = os.getenv("SUPABASE_URL", "https://dudpqqsguexasxbrvykn.supabase.co")
SUPABASE_KEY = os.getenv(
    "SUPABASE_KEY",
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImR1ZHBxcXNndWV4YXN4YnJ2eWtuIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NTc4OTUwOTYsImV4cCI6MjA3MzQ3MTA5Nn0.YTA9O0CUfrUpqXbRTQOb3IpcX0Fk3HyWjOFK23zM3zs",
)
SUPABASE_TABLE = f"{SUPABASE_URL}/rest/v1/padroes"

SUPABASE_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    # "Prefer": "return=representation"  # se quiser que retorne o registro salvo
    "Prefer": "return=minimal",
}


def save_to_supabase(payload: dict):
    """
    Insere 1 linha na tabela pattern_results via REST.
    Espera um dict com: id_padrao, nome_padrao, hora, win, loss
    Mapeia para colunas: pattern_id, pattern_name, ts, win, loss, gale, color, root_id
    """
    body = {
        "pattern_id": payload["id_padrao"],
        "pattern_name": payload["nome_padrao"],
        "ts": payload["hora"],  # ISO UTC (ok)
        "win": payload["win"],
        "loss": payload["loss"],
        # opcionais (coloque se tiver esses dados no seu payload/threads)
        # "gale": payload.get("gale", 0),
        # "color": payload.get("color"),
        # "root_id": payload.get("root_id"),
    }
    try:
        resp = requests.post(
            SUPABASE_TABLE, headers=SUPABASE_HEADERS, json=body, timeout=10
        )
        if resp.status_code >= 300:
            print(f"[SUPABASE] Falha {resp.status_code}: {resp.text}")
        else:
            print("[SUPABASE] Inserido com sucesso.")
    except Exception as e:
        print(f"[SUPABASE] Erro: {e}")


# Compila regex tolerando espaços extras e variações de caixa
def _rx(name: str):
    esc = re.escape(name)
    esc = re.sub(r"\\\s\+", r"\\s+", esc)  # (não necessário, só por segurança)
    esc = esc.replace(r"\ ", r"\s*")  # permite 0+ espaços onde há espaço no nome
    return re.compile(rf"\b{esc}\b", re.IGNORECASE)


RX_PATTERNS = [(pid, name, _rx(name)) for pid, name in REG_PATTERNS.items()]

# ===================== REGEX AUXILIARES =====================
RE_HEADER_HINT = re.compile(r"POSS[ÍI]VEL\s+PADR[ÃA]O\s+IDENTIFICADO", re.I)
RE_WIN = re.compile(r"\bWIN+\b", re.I)
RE_LOSS = re.compile(r"\bLOSS+\b", re.I)
RE_ABORT = re.compile(r"\bABORTAD[OA]\b", re.I)
RE_GALE = re.compile(r"GALE\s*(\d+)", re.I)

# ========================= ESTADO ===========================
threads = {}  # root_id -> dict


acre_tz = pytz.timezone("America/Rio_Branco")


def iso_now():
    """Retorna hora atual no fuso do Acre em formato ISO-8601."""
    return datetime.now(acre_tz).isoformat()


def find_pattern_in(texto: str):
    """Retorna (id_padrao, nome_padrao) se achar na lista, senão (None, None)."""
    if not texto:
        return (None, None)
    for pid, name, rx in RX_PATTERNS:
        if rx.search(texto):
            return (pid, name)
    return (None, None)


def terminal_event(texto: str):
    if RE_ABORT.search(texto):
        return "aborted"
    if RE_WIN.search(texto):
        return "win"
    if RE_LOSS.search(texto):
        return "loss"
    return None


@client.on(events.NewMessage(chats=TARGET))
async def on_msg(event):
    msg = event.message
    texto = msg.message or ""
    mid = msg.id
    rid = msg.reply_to_msg_id

    # ===== 1) Mensagem RAIZ? =====
    # Heurística: contém “possível padrão identificado” OU contém explicitamente um nome conhecido
    root_like = (RE_HEADER_HINT.search(texto) is not None) or (
        find_pattern_in(texto)[0] is not None
    )

    if root_like and not rid:
        id_padrao, nome_padrao = find_pattern_in(texto)
        if id_padrao is None:
            # raiz sem nome que conste na lista — apenas loga e não cria thread
            print(
                f"[ROOT? {mid}] Texto parece raiz mas não bate com lista: {texto[:80]!r}"
            )
            return

        threads[mid] = {
            "id_padrao": id_padrao,
            "nome_padrao": nome_padrao,
            "created_at": iso_now(),
            "gales": 0,
            "status": None,
            "mensagens": [{"id": mid, "texto": texto, "at": iso_now()}],
        }
        print("\n" + "=" * 60)
        print(f"[ROOT {mid}] padrão='{nome_padrao}' (id={id_padrao})")
        print("=" * 60)
        return

    # ===== 2) Resposta a uma raiz =====
    if rid:
        root_id = rid
        # cria casca se ainda não existe
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

        # backfill do nome/id a partir da mensagem raiz real
        if t["id_padrao"] is None:
            try:
                root_msg = await event.get_reply_message()
                if root_msg:
                    rtext = root_msg.message or ""
                    pid, pname = find_pattern_in(rtext)
                    if pid is not None:
                        t["id_padrao"], t["nome_padrao"] = pid, pname
                        # opcional: guardar a raiz também
                        if not any(m["id"] == root_msg.id for m in t["mensagens"]):
                            t["mensagens"].insert(
                                0, {"id": root_msg.id, "texto": rtext, "at": iso_now()}
                            )
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

        # Desfecho
        end = terminal_event(texto)
        if end:
            t["status"] = end

            # abortado = ignora
            if end == "aborted":
                print(f"[CLOSE {root_id}] ❌ abortado (não salva)\n")
                del threads[root_id]
                return

            # só salva se soubermos qual padrão é (está na lista)
            if t["id_padrao"] is None:
                print(
                    f"[CLOSE {root_id}] ⚠️ sem id/nome de padrão da lista — não salva.\n"
                )
                del threads[root_id]
                return

            payload = {
                "id_padrao": t["id_padrao"],
                "nome_padrao": t["nome_padrao"],
                "hora": iso_now(),
                "win": 1 if end == "win" else 0,
                "loss": 1 if end == "loss" else 0,
            }

            print(f"[CLOSE {root_id}] ✅ concluído — pronto para o banco:")
            print(json.dumps(payload, indent=2, ensure_ascii=False))
            # Envia para o Supabase
            save_to_supabase(payload)

            # >>> AQUI você persiste (Supabase/SQL/HTTP) <<<
            # ex.: requests.post(SUA_URL, json=payload)

            del threads[root_id]
            return


print("Ouvindo mensagens... Ctrl+C para sair.")
client.start()
client.run_until_disconnected()
