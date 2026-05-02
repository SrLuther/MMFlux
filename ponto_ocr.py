"""Modulo OCR para leitura de comprovantes de ponto eletronico.

Motor primario: Google Gemini Vision API (gemini-2.0-flash).
Fallback:       Tesseract local (requer instalacao separada).

Configure GOOGLE_VISION_KEY no .env com a chave do Google AI Studio / Gemini API.
Se a variavel estiver vazia ou ausente, o sistema usa Tesseract.
"""
# pyright: reportMissingTypeStubs=false

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass
from typing import Optional

import requests


# ---------------------------------------------------------------------------
# Gemini Vision API
# ---------------------------------------------------------------------------

_GEMINI_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-flash-lite-latest:generateContent"
)

_GEMINI_PROMPT = (
    "Você é um extrator de dados de comprovantes de ponto eletrônico brasileiro.\n"
    "Analise a imagem e extraia os seguintes campos:\n"
    "- CPF: apenas os 11 dígitos, sem pontos ou traços\n"
    "- NSR: número sequencial de registro (apenas dígitos)\n"
    "- DATA: no formato DD/MM/AAAA\n"
    "- HORA: no formato HH:MM\n"
    "- NOME: nome completo do funcionário\n"
    "- NREP: número do equipamento (apenas dígitos, pode estar ausente)\n\n"
    "Responda SOMENTE com JSON válido no formato:\n"
    '{"cpf":"","nsr":"","data":"","hora":"","nome":"","nrep":""}\n'
    "Se não encontrar um campo, deixe a string vazia."
)


def _ocr_via_gemini(image_path: str) -> PontoData:
    """Envia imagem para Gemini Vision API e retorna PontoData preenchido."""
    api_key = os.getenv("GOOGLE_VISION_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GOOGLE_VISION_KEY nao configurado.")

    with open(image_path, "rb") as fh:
        content_b64 = base64.b64encode(fh.read()).decode("utf-8")

    # Detecta mime type pela extensao
    ext = os.path.splitext(image_path)[-1].lower()
    mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
            "webp": "image/webp", "gif": "image/gif"}.get(ext.lstrip("."), "image/jpeg")

    payload = {
        "contents": [
            {
                "parts": [
                    {"text": _GEMINI_PROMPT},
                    {"inline_data": {"mime_type": mime, "data": content_b64}},
                ]
            }
        ],
        "generationConfig": {"temperature": 0, "maxOutputTokens": 256},
    }

    resp = requests.post(
        _GEMINI_ENDPOINT,
        params={"key": api_key},
        headers={"Content-Type": "application/json"},
        data=json.dumps(payload),
        timeout=30,
    )
    resp.raise_for_status()

    data = resp.json()
    try:
        raw_text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        return PontoData(error="Gemini nao retornou texto.")

    # Extrai JSON da resposta (Gemini pode adicionar markdown ao redor)
    json_match = re.search(r'\{[^}]+\}', raw_text, re.DOTALL)
    if not json_match:
        return PontoData(raw_text=raw_text, error="Gemini retornou formato inesperado.")

    try:
        fields = json.loads(json_match.group())
    except json.JSONDecodeError:
        return PontoData(raw_text=raw_text, error="JSON invalido na resposta do Gemini.")

    cpf  = re.sub(r"\D", "", fields.get("cpf", ""))
    nsr  = re.sub(r"\D", "", fields.get("nsr", ""))
    data_val  = fields.get("data", "").strip()
    hora = fields.get("hora", "").strip()
    nome = fields.get("nome", "").strip()
    nrep = re.sub(r"\D", "", fields.get("nrep", ""))

    error: Optional[str] = None
    if not cpf:
        error = "CPF nao encontrado no comprovante."
    elif not data_val:
        error = "DATA nao encontrada no comprovante."
    elif not hora:
        error = "HORA nao encontrada no comprovante."
    elif not nsr:
        error = "NSR nao encontrado no comprovante."

    return PontoData(
        nome=nome, cpf=cpf, data=data_val, hora=hora,
        nsr=nsr, nrep=nrep, raw_text=raw_text, error=error,
    )


# ---------------------------------------------------------------------------
# Deteccao automatica do Tesseract (Linux e Windows)
# ---------------------------------------------------------------------------

def _find_tesseract() -> Optional[str]:
    """Localiza o executavel tesseract automaticamente, sem configuracao manual.

    Ordem de busca:
    1. Variavel de ambiente TESSERACT_CMD (override manual se necessario)
    2. shutil.which — PATH do sistema (funciona em qualquer plataforma)
    3. Caminhos fixos comuns no Windows
    4. Caminhos fixos comuns no Linux/Mac
    """
    env_cmd = os.getenv("TESSERACT_CMD", "").strip()
    if env_cmd and os.path.isfile(env_cmd):
        return env_cmd

    which_result = shutil.which("tesseract")
    if which_result:
        return which_result

    if sys.platform == "win32":
        win_candidates = [
            r"C:\Program Files\Tesseract-OCR\tesseract.exe",
            r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
            os.path.join(
                os.environ.get("LOCALAPPDATA", ""),
                "Programs", "Tesseract-OCR", "tesseract.exe",
            ),
            os.path.join(
                os.environ.get("APPDATA", ""),
                "Tesseract-OCR", "tesseract.exe",
            ),
        ]
        for p in win_candidates:
            if p and os.path.isfile(p):
                return p
    else:
        unix_candidates = [
            "/usr/bin/tesseract",
            "/usr/local/bin/tesseract",
            "/opt/homebrew/bin/tesseract",
        ]
        for p in unix_candidates:
            if os.path.isfile(p):
                return p

    return None


try:
    import pytesseract  # type: ignore[import-untyped]
    from PIL import Image, ImageEnhance, ImageOps  # type: ignore[import-untyped]

    _tess_path = _find_tesseract()
    if _tess_path:
        pytesseract.pytesseract.tesseract_cmd = _tess_path

    _OCR_AVAILABLE = True
    _OCR_MISSING_TESS = _tess_path is None
except ImportError:
    _OCR_AVAILABLE = False
    _OCR_MISSING_TESS = False


@dataclass
class PontoData:
    """Dados extraidos de um comprovante de ponto eletronico."""

    nome: str = ""
    cpf: str = ""
    data: str = ""
    hora: str = ""
    nsr: str = ""
    nrep: str = ""
    ad_key: str = ""
    raw_text: str = ""
    error: Optional[str] = None


def _preprocess(img: "Image.Image") -> "Image.Image":
    """Prepara imagem para OCR: corrige rotacao EXIF, normaliza tamanho e contraste."""
    # Respeita orientacao EXIF (fotos de celular ficam de lado sem isso)
    img = ImageOps.exif_transpose(img)
    gray = ImageOps.grayscale(img)
    # Fotos de celular chegam com >3000px — reduz para largura util de 1600px
    MAX_W = 1600
    w, h = gray.size
    if w > MAX_W:
        gray = gray.resize((MAX_W, int(h * MAX_W / w)), Image.LANCZOS)
    gray = ImageOps.autocontrast(gray, cutoff=1)
    gray = ImageEnhance.Sharpness(gray).enhance(2.0)
    return gray


def _find(pattern: str, text: str) -> str:
    """Aplica regex case-insensitive e retorna o grupo 1 ou string vazia."""
    m = re.search(pattern, text, re.IGNORECASE)
    return m.group(1).strip() if m else ""


def parse_text(raw: str) -> PontoData:
    """Faz o parse do texto OCR do comprovante de ponto eletronico.

    A impressora termica quebra o texto em colunas fixas (~35 chars).
    Ao concatenar as linhas SEM espaco, as palavras partidas se reconstituem
    corretamente (ex: 'SANT' + 'OS' -> 'SANTOS').
    """
    # Remove ruido de margem antes de juntar:
    # - margem direita: "— EE", "— ÉA" etc.
    # - margem esquerda: "& ", e letras de borda isoladas como "E " no inicio da linha
    clean_lines = []
    for ln in raw.splitlines():
        ln = re.sub(r'\s*[\u2014\u2013\-]{1,2}\s*[A-Za-z\u00c0-\u00ff]{0,5}$', '', ln)  # direita
        ln = re.sub(r'^[&!*|]+\s*', '', ln)              # "& " no inicio
        ln = re.sub(r'^[A-Z]\s(?=[A-Z])', '', ln)        # letra solta de borda: "E S" -> "S"
        clean_lines.append(ln)
    text = "".join(clean_lines)
    # Colapsa espacos multiplos preservando os que existem dentro de valores
    text = re.sub(r" {2,}", " ", text)

    # NOME: entre "NOME:" e "CPF:" (CPF pode ser lido como CPE, CPG...)
    nome = _find(r"NOME\s*:?\s*(.+?)(?=CP[FfEeGg]\s*:?)", text)

    # CPF: aceita 000.000.000-00 ou 00000000000
    # Erros OCR comuns: O/o→0, G/g→6, I/l→1, B→8, b→6, S→5, Z/z→2, q→9, d→0
    # G maiusculo confundido com 6 em impressora termica
    _OCR_DIGIT = str.maketrans("OoGgIlBbSZzqd", "0066118652290")
    cpf_raw = _find(r"CP[FfEeGg]\s*:?\s*([0OoGgIlBb\d][\d\s.OoGgIlBb\-]{8,13}[0OoGgIlBb\d])", text)
    cpf = re.sub(r"\D", "", cpf_raw.translate(_OCR_DIGIT))

    # NSR: "NSR"/"NSF"/"NSP" — tolera até 20 chars; P confundido com R na impressora
    _OCR_NUM = str.maketrans("OoGgIlBbSZzqd", "0066118652290")
    nsr_raw = _find(r"NS[RrFfBbPp]?\s*:?\s*([\w\s]{3,20})(?=\s*(?:DATA|HORA|NOME|$))", text)
    if nsr_raw:
        nsr = re.sub(r"\D", "", nsr_raw.translate(_OCR_NUM))
    else:
        nsr = ""

    # NREP: numero do equipamento
    nrep_raw = _find(r"NREP\s*:?\s*([\d\s]+?)(?=MODELO\s*:|$)", text)
    nrep = re.sub(r"\D", "", nrep_raw)

    # DATA: DD/MM/YYYY
    data = _find(r"DATA\s*:?\s*(\d{2}/\d{2}/\d{4})", text)

    # HORA: HH:MM — aceita "10:00" ou "1000" (OCR perde os dois-pontos)
    hora_raw = _find(r"HORA\s*:?\s*(\d{1,2}:?\d{2})(?!\d)", text)
    if hora_raw and ":" not in hora_raw and len(hora_raw) == 4:
        hora = hora_raw[:2] + ":" + hora_raw[2:]
    elif hora_raw and ":" not in hora_raw and len(hora_raw) == 3:
        hora = hora_raw[0] + ":" + hora_raw[1:]
    else:
        hora = hora_raw

    # AD (chave de autenticidade): tudo apos "AD:" ate o fim
    ad_key = _find(r"\bAD\s*:?\s*(.+)$", text)

    error: Optional[str] = None
    if not cpf:
        error = "CPF nao encontrado no comprovante."
    elif not data:
        error = "DATA nao encontrada no comprovante."
    elif not hora:
        error = "HORA nao encontrada no comprovante."
    elif not nsr:
        error = "NSR nao encontrado no comprovante."

    return PontoData(
        nome=nome,
        cpf=cpf,
        data=data,
        hora=hora,
        nsr=nsr,
        nrep=nrep,
        ad_key=ad_key,
        raw_text=raw,
        error=error,
    )


def ocr_image(path: str, use_vision: bool = True) -> PontoData:
    """Executa OCR em uma imagem e retorna os dados do comprovante.

    use_vision=True  → tenta Gemini Vision primeiro (alta acuracia, requer chave).
    use_vision=False → usa Tesseract diretamente (rapido, offline, para frames ao vivo).
    """
    # --- Tenta Gemini ---
    vision_key = os.getenv("GOOGLE_VISION_KEY", "").strip()
    if use_vision and vision_key:
        try:
            return _ocr_via_gemini(path)
        except requests.HTTPError as exc:
            fallback_reason = f"Gemini erro HTTP {exc.response.status_code}: {exc.response.text[:200]}"
        except Exception as exc:  # noqa: BLE001
            fallback_reason = f"Gemini indisponivel: {exc}"
    else:
        fallback_reason = "GOOGLE_VISION_KEY nao configurado"

    # --- Fallback: Tesseract ---
    if not _OCR_AVAILABLE:
        return PontoData(
            error=(
                f"Google Vision: {fallback_reason}. "
                "Tesseract tambem nao esta instalado."
            )
        )

    if _OCR_MISSING_TESS:
        return PontoData(
            error=(
                f"Google Vision: {fallback_reason}. "
                "Tesseract nao encontrado no sistema."
            )
        )

    try:
        img = Image.open(path)
    except Exception as exc:  # noqa: BLE001
        return PontoData(error=f"Erro ao abrir imagem: {exc}")

    try:
        processed = _preprocess(img)

        _KEYWORDS = ("CPF", "NSR", "NOME", "DATA", "HORA", "NREP")
        _LANGS = ["por", "eng"]
        _PSMS  = [4, 6]

        best_text  = ""
        best_score = -1

        for lang in _LANGS:
            for psm in _PSMS:
                try:
                    t: str = pytesseract.image_to_string(
                        processed, lang=lang,
                        config=f"--oem 3 --psm {psm}",
                    )
                    score = sum(1 for kw in _KEYWORDS if kw in t.upper())
                    if score > best_score:
                        best_score = score
                        best_text  = t
                    if best_score >= 4:
                        break
                except pytesseract.TesseractError:
                    continue
            if best_score >= 4:
                break

        raw_text = best_text
    except Exception as exc:  # noqa: BLE001
        return PontoData(error=f"Erro no OCR (Tesseract): {exc}")

    return parse_text(raw_text)
