"""Gateway de notificacoes via WhatsApp para o MultiMax Fluxos."""
from __future__ import annotations

import base64
import logging
import os
import threading
from decimal import Decimal

import requests  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)

_DEFAULT_URL = "https://www.multimax.tec.br/notify"
_FALLBACKS = [
    "http://127.0.0.1:3001/notify",
    "http://localhost:3001/notify",
]


def _notify_url() -> str:
    return os.getenv("WHATSAPP_NOTIFY_URL", _DEFAULT_URL).rstrip("/")


def _timeout() -> float:
    try:
        return float(os.getenv("WHATSAPP_NOTIFY_TIMEOUT", "8"))
    except (TypeError, ValueError):
        return 8.0


def _candidate_urls() -> list[str]:
    base = _notify_url()
    urls = [base]
    for fb in _FALLBACKS:
        if fb not in urls:
            urls.append(fb)
    return urls


def _post(
    mensagem: str, origin: str = "fluxos", extra: dict | None = None
) -> None:
    """Tenta enviar mensagem para cada URL candidata; falha silenciosamente."""
    payload: dict = {"mensagem": mensagem, "origin": origin}
    if extra:
        payload.update(extra)
    headers = {"Content-Type": "application/json"}
    for url in _candidate_urls():
        try:
            resp = requests.post(
                url, json=payload, headers=headers, timeout=_timeout()
            )
            if resp.status_code < 400:
                logger.info("WhatsApp notify ok via %s", url)
                return
            logger.warning(
                "WhatsApp notify %s retornou %s", url, resp.status_code
            )
        except requests.RequestException as exc:
            logger.warning("WhatsApp notify falhou em %s: %s", url, exc)
    logger.error("WhatsApp notify falhou em todos os endpoints")


_LINK = "🔗 https://multimax.tec.br"


def send(mensagem: str, origin: str = "fluxos") -> None:
    """Envia notificacao em thread separada para nao bloquear a requisicao."""
    mensagem = (mensagem or "").strip()
    if not mensagem:
        return
    mensagem = f"{mensagem}\n\n{_LINK}"
    t = threading.Thread(target=_post, args=(mensagem, origin), daemon=True)
    t.start()


def send_pdf(pdf_bytes: bytes, filename: str, mes_label: str) -> None:
    """Envia PDF como documento para o grupo Notify via WhatsApp."""
    if not pdf_bytes:
        return
    b64 = base64.b64encode(pdf_bytes).decode()
    caption = f"📄 *Resumo {mes_label} — MultiMax Fluxos*\n\n{_LINK}"
    extra = {"arquivo_base64": b64, "nome_arquivo": filename}
    t = threading.Thread(
        target=_post, args=(caption, "fluxos_pdf", extra), daemon=True
    )
    t.start()


# ── helpers de formatacao ──────────────────────────────────────────────────

def _fmt_horas(valor: Decimal | float | str) -> str:
    v = float(valor)
    sinal = "+" if v >= 0 else ""
    return f"{sinal}{v:.1f}h"


def _fmt_moeda(valor: Decimal | float | None) -> str:
    if valor is None:
        return "-"
    v = (
        f"{float(valor):,.2f}"
        .replace(",", "X")
        .replace(".", ",")
        .replace("X", ".")
    )
    return f"R$ {v}"


def entry_criado(
    collab_name: str,
    role: str,
    entry_date: str,
    hours: Decimal,
    note: str | None = None,
) -> None:
    sinal = "+" if float(hours) >= 0 else ""
    linhas = [
        "⏱️ *Novo lançamento — MultiMax Fluxos*",
        f"👤 {collab_name} ({role})",
        f"📅 {entry_date}",
        f"🕐 {sinal}{float(hours):.1f}h",
    ]
    if note:
        linhas.append(f"📝 {note}")
    send("\n".join(linhas), origin="fluxos_entry_criado")


def entry_atualizado(
    collab_name: str,
    entry_date: str,
    hours: Decimal,
    note: str | None = None,
) -> None:
    sinal = "+" if float(hours) >= 0 else ""
    linhas = [
        "✏️ *Lançamento atualizado — MultiMax Fluxos*",
        f"👤 {collab_name}",
        f"📅 {entry_date}",
        f"🕐 {sinal}{float(hours):.1f}h",
    ]
    if note:
        linhas.append(f"📝 {note}")
    send("\n".join(linhas), origin="fluxos_entry_atualizado")


def entry_removido(collab_name: str, entry_date: str, hours: Decimal) -> None:
    sinal = "+" if float(hours) >= 0 else ""
    send(
        f"🗑️ *Lançamento removido — MultiMax Fluxos*\n"
        f"👤 {collab_name}\n"
        f"📅 {entry_date}  {sinal}{float(hours):.1f}h",
        origin="fluxos_entry_removido",
    )


def colaborador_criado(
    name: str,
    role: str | None,
    daily_rate: Decimal | None = None,
) -> None:
    cargo = f" ({role})" if role else ""
    send(
        f"👥 *Novo colaborador — MultiMax Fluxos*\n"
        f"{name}{cargo}\n"
        f"💵 Diária: {_fmt_moeda(daily_rate)}",
        origin="fluxos_colaborador_criado",
    )


def colaborador_toggle(name: str, ativo: bool) -> None:
    status = "✅ ativado" if ativo else "⛔ desativado"
    send(
        f"👥 *Colaborador {status} — MultiMax Fluxos*\n{name}",
        origin="fluxos_colaborador_toggle",
    )


def colaborador_atualizado(
    old_name: str,
    new_name: str,
    old_role: str | None,
    new_role: str | None,
    old_daily_rate: Decimal | None = None,
    new_daily_rate: Decimal | None = None,
) -> None:
    old_role_txt = old_role or "-"
    new_role_txt = new_role or "-"
    send(
        "✏️ *Colaborador atualizado — MultiMax Fluxos*\n"
        f"Nome: {old_name} → {new_name}\n"
        f"Função: {old_role_txt} → {new_role_txt}\n"
        f"Diária: {_fmt_moeda(old_daily_rate)} → {_fmt_moeda(new_daily_rate)}",
        origin="fluxos_colaborador_atualizado",
    )


def resumo_geral(
    cards: list[dict],
    totals: dict,
) -> None:
    """Envia resumo geral de todos os colaboradores do mes para o grupo."""
    linhas = [
        "📊 *Resumo Geral — MultiMax Fluxos*",
        f"📅 {totals['month_label']}",
        "",
    ]
    for c in cards:
        net = float(c["net"])
        sinal = "+" if net >= 0 else ""
        pos = float(c["positive"])
        neg = float(c["negative"])
        linhas.append(
            f"👤 *{c['name']}* ({c['role']})\n"
            f"   ➕ {pos:.1f}h  ➖ {neg:.1f}h"
            f" = *{sinal}{net:.1f}h* ({c['days']}d)\n"
        )
    total_pos = float(totals["positive"])
    total_neg = float(totals["negative"])
    total_net = float(totals["net"])
    linhas += [
        "",
        "─────────────────",
        f"Total bruto:    {total_pos:.1f}h",
        f"Total descontos: {total_neg:.1f}h",
        f"*Total líquido:  {total_net:.1f}h ({totals['days']} dias)*",
    ]
    send("\n".join(linhas), origin="fluxos_resumo_geral")
