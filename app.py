"""Aplicacao Flask para controle de fluxos de horas por colaborador."""
# pyright: reportMissingTypeStubs=false
# pylint: disable=no-member

from __future__ import annotations

import calendar
import json
import os
import re
import secrets
import string
import threading
import time
import uuid
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from functools import wraps
from typing import Any, ClassVar, cast

from dotenv import load_dotenv
from flask import (
    Flask,
    flash,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    session as _flask_session,
    url_for,
)
from flask_login import (  # type: ignore[import-untyped]
    LoginManager,
    UserMixin,
    current_user,
    login_required,
    login_user,
    logout_user,
)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func, text
from sqlalchemy.orm import DeclarativeBase
from werkzeug.security import check_password_hash, generate_password_hash

import notify as wz
import ponto_ocr

MESES_PT = ['', 'Janeiro', 'Fevereiro', 'Março', 'Abril', 'Maio', 'Junho',
            'Julho', 'Agosto', 'Setembro', 'Outubro', 'Novembro', 'Dezembro']


class Base(DeclarativeBase):
    """Base declarativa para os modelos SQLAlchemy."""

    query: ClassVar[Any]


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

_MESES_PT = [
    "", "Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho",
    "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro",
]
DB_DIR = os.path.join(BASE_DIR, ".db")
os.makedirs(DB_DIR, exist_ok=True)
DB_PATH = os.path.join(DB_DIR, "fluxos_zero.db")

UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads", "ponto")
_ALLOWED_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".jfif"}

# Constante de jornada padrão: 7h20 = 440 minutos
JORNADA_MIN = 440

# ---------------------------------------------------------------------------
# Alertas agendados por horário definido pelo colaborador
# ---------------------------------------------------------------------------
# Chave: (collab_id, date_iso, punch_type_esperado)
# Valor: {fire_at, whatsapp, collab_name, expected_time, tipo, data_str}
_pending_alerts: dict[tuple, dict] = {}
_pending_alerts_lock = threading.Lock()

app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["SECRET_KEY"] = os.getenv("FLUXOS_SECRET", "trocar-esta-chave")
app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{DB_PATH}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["MAX_CONTENT_LENGTH"] = 15 * 1024 * 1024  # 15 MB
app.config["TEMPLATES_AUTO_RELOAD"] = True


db = SQLAlchemy(model_class=Base)
db.init_app(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"  # type: ignore[assignment]
Base.query = db.session.query_property()



@app.template_filter("hhmm")
def hhmm_filter(value: Any) -> str:
    """Converte horas decimais para formato legivel: 7.38 -> '7h23'."""
    try:
        total_min = round(float(value) * 60)
        sign = "-" if total_min < 0 else ""
        total_min = abs(total_min)
        h, m = divmod(total_min, 60)
        return f"{sign}{h}h{m:02d}" if m else f"{sign}{h}h"
    except (TypeError, ValueError):
        return str(value)


@app.template_filter("mask_cpf")
def mask_cpf_filter(cpf: Any) -> str:
    """Mascara CPF para exibição pública: 00x.xxx.xxx-00."""
    import re as _re
    digits = _re.sub(r"\D", "", str(cpf or ""))
    if len(digits) != 11:
        return str(cpf) if cpf else "—"
    return f"{digits[0:2]}x.xxx.xxx-{digits[9:11]}"


class User(Base, UserMixin):
    """Usuario administrador para operacoes de escrita."""

    __tablename__ = "user"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(
        db.String(60),
        unique=True,
        nullable=False,
        index=True,
    )
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        nullable=False,
    )


class Collaborator(Base):
    """Colaborador com saldo de horas no modulo de fluxos."""

    __tablename__ = "collaborator"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, index=True)
    role = db.Column(db.String(120), nullable=True)
    daily_rate = db.Column(db.Numeric(10, 2), nullable=True)
    cpf = db.Column(db.String(20), nullable=True, index=True)
    ponto_password_hash = db.Column(db.String(255), nullable=True)
    active = db.Column(db.Boolean, nullable=False, default=True)
    folga_days = db.Column(db.Integer, nullable=False, default=0)
    whatsapp = db.Column(db.String(30), nullable=True)
    schedule_json = db.Column(db.Text, nullable=True)
    created_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        nullable=False,
    )


class HourEntry(Base):
    """Lancamento de horas por colaborador e data."""

    __tablename__ = "hour_entry"

    id = db.Column(db.Integer, primary_key=True)
    collaborator_id = db.Column(
        db.Integer,
        db.ForeignKey("collaborator.id"),
        nullable=False,
        index=True,
    )
    entry_date = db.Column(db.Date, nullable=False, index=True)
    hours = db.Column(db.Numeric(6, 2), nullable=False)
    note = db.Column(db.String(255), nullable=True)
    archived = db.Column(db.Boolean, nullable=False, default=False)
    gives_folga = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        nullable=False,
    )
    updated_at = db.Column(db.DateTime, nullable=True)

    collaborator = db.relationship(
        "Collaborator",
        backref=db.backref(
            "entries",
            lazy=True,
            cascade="all, delete-orphan",
        ),
    )


class PunchRecord(Base):
    """Registro de batida de ponto lido via OCR do comprovante."""

    __tablename__ = "punch_record"

    id = db.Column(db.Integer, primary_key=True)
    collaborator_id = db.Column(
        db.Integer,
        db.ForeignKey("collaborator.id"),
        nullable=True,
        index=True,
    )
    raw_cpf = db.Column(db.String(20), nullable=False)
    raw_name = db.Column(db.String(180), nullable=False)
    punch_date = db.Column(db.Date, nullable=False, index=True)
    punch_time = db.Column(db.Time, nullable=False)
    nsr = db.Column(db.String(30), unique=True, nullable=False)
    nrep = db.Column(db.String(30), nullable=True)
    ad_key = db.Column(db.String(600), nullable=True)
    image_filename = db.Column(db.String(255), nullable=True)
    processed = db.Column(db.Boolean, nullable=False, default=False)
    # Tipo da batida: entrada|intervalo_saida|intervalo_retorno|saida_final|extra
    punch_type = db.Column(db.String(30), nullable=True)
    # Origem: automatico (OCR/camera) | manual | admin
    origin = db.Column(db.String(20), nullable=True, default='automatico')
    # Marcado como direito a folga pelo colaborador/admin
    gives_folga = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        nullable=False,
    )

    collaborator = db.relationship(
        "Collaborator",
        backref=db.backref("punches", lazy=True),
    )


class Setting(Base):
    """Configuracoes persistentes em chave-valor."""

    __tablename__ = "setting"

    key = db.Column(db.String(60), primary_key=True)
    value = db.Column(db.String(255), nullable=False)


class Holiday(Base):
    """Feriado cadastrado pelo administrador."""

    __tablename__ = "holiday"

    id = db.Column(db.Integer, primary_key=True)
    holiday_date = db.Column(db.Date, nullable=False, unique=True, index=True)
    descricao = db.Column(db.String(200), nullable=False)
    ativo = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class PontoAjuste(Base):
    """Ajuste de saldo de ponto: desconto de horas extras ou uso de folga."""

    __tablename__ = "ponto_ajuste"

    id = db.Column(db.Integer, primary_key=True)
    collaborator_id = db.Column(
        db.Integer, db.ForeignKey("collaborator.id"), nullable=False, index=True
    )
    # 'desconto_extra' | 'uso_folga'
    tipo = db.Column(db.String(20), nullable=False)
    # Minutos descontados (para desconto_extra) ou JORNADA_MIN (para uso_folga)
    minutos = db.Column(db.Integer, nullable=False, default=0)
    data_referencia = db.Column(db.Date, nullable=True)
    obs = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    # 'colaborador' | 'admin'
    criado_por = db.Column(db.String(20), nullable=False, default='colaborador')

    collaborator = db.relationship(
        "Collaborator",
        backref=db.backref("ajustes_ponto", lazy=True),
    )


@login_manager.user_loader
def load_user(user_id: str) -> User | None:
    """Carrega o usuario autenticado pelo identificador salvo na sessao."""
    user: User | None = cast(User | None, db.session.get(User, int(user_id)))
    return cast(User | None, user)


def ensure_admin() -> None:
    """Cria o usuario administrador padrao caso ainda nao exista."""
    admin_username = os.getenv("FLUXOS_ADMIN_USER", "admin")
    admin_password = os.getenv("FLUXOS_ADMIN_PASS", "admin123")

    existing = User.query.filter_by(username=admin_username).first()
    if existing:
        return

    db.session.add(
        User(
            username=admin_username,
            password_hash=generate_password_hash(admin_password),
        )
    )
    db.session.commit()


def ensure_schema() -> None:
    """Aplica migracoes simples de schema para bancos existentes."""
    columns = db.session.execute(
        text("PRAGMA table_info(collaborator)")
    ).mappings().all()
    col_names = {str(col["name"]) for col in columns}
    if "daily_rate" not in col_names:
        db.session.execute(
            text(
                "ALTER TABLE collaborator "
                "ADD COLUMN daily_rate NUMERIC(10, 2)"
            )
        )
        db.session.commit()
    if "cpf" not in col_names:
        db.session.execute(
            text("ALTER TABLE collaborator ADD COLUMN cpf VARCHAR(20)")
        )
        db.session.commit()

    # collaborator.ponto_password_hash (adicionado em v0.6.0)
    if 'ponto_password_hash' not in col_names:
        db.session.execute(
            text('ALTER TABLE collaborator ADD COLUMN ponto_password_hash VARCHAR(255)')
        )
        db.session.commit()

    # punch_record.processed (adicionado em v0.5.1)
    pr_cols = db.session.execute(
        text("PRAGMA table_info(punch_record)")
    ).mappings().all()
    pr_col_names = {str(c["name"]) for c in pr_cols}
    if pr_col_names and "processed" not in pr_col_names:
        db.session.execute(
            text("ALTER TABLE punch_record ADD COLUMN processed BOOLEAN NOT NULL DEFAULT 0")
        )
        db.session.commit()

    # hour_entry.archived (adicionado em v0.7.0)
    he_cols = db.session.execute(
        text("PRAGMA table_info(hour_entry)")
    ).mappings().all()
    he_col_names = {str(c["name"]) for c in he_cols}
    if he_col_names and "archived" not in he_col_names:
        db.session.execute(
            text("ALTER TABLE hour_entry ADD COLUMN archived BOOLEAN NOT NULL DEFAULT 0")
        )
        db.session.commit()

    # hour_entry.gives_folga (adicionado em v0.8.0)
    if he_col_names and "gives_folga" not in he_col_names:
        db.session.execute(
            text("ALTER TABLE hour_entry ADD COLUMN gives_folga BOOLEAN NOT NULL DEFAULT 0")
        )
        db.session.commit()

    # collaborator.folga_days (adicionado em v0.8.0)
    if "folga_days" not in col_names:
        db.session.execute(
            text("ALTER TABLE collaborator ADD COLUMN folga_days INTEGER NOT NULL DEFAULT 0")
        )
        db.session.commit()

    # collaborator.whatsapp (adicionado em v1.0.0)
    if "whatsapp" not in col_names:
        db.session.execute(
            text("ALTER TABLE collaborator ADD COLUMN whatsapp VARCHAR(30)")
        )
        db.session.commit()

    # collaborator.schedule_json (adicionado em v1.0.0)
    if "schedule_json" not in col_names:
        db.session.execute(
            text("ALTER TABLE collaborator ADD COLUMN schedule_json TEXT")
        )
        db.session.commit()

    # punch_record.punch_type (adicionado em v1.0.0)
    if pr_col_names and "punch_type" not in pr_col_names:
        db.session.execute(
            text("ALTER TABLE punch_record ADD COLUMN punch_type VARCHAR(30)")
        )
        db.session.commit()

    # punch_record.origin (adicionado em v1.0.0)
    if pr_col_names and "origin" not in pr_col_names:
        db.session.execute(
            text("ALTER TABLE punch_record ADD COLUMN origin VARCHAR(20) DEFAULT 'automatico'")
        )
        db.session.commit()

    # punch_record.gives_folga (adicionado em v1.1.0)
    if pr_col_names and "gives_folga" not in pr_col_names:
        db.session.execute(
            text("ALTER TABLE punch_record ADD COLUMN gives_folga BOOLEAN NOT NULL DEFAULT 0")
        )
        db.session.commit()

    # holiday.ativo (adicionado em v0.10.8)
    h_cols = db.session.execute(text("PRAGMA table_info(holiday)")).mappings().all()
    h_col_names = {str(c["name"]) for c in h_cols}
    if h_col_names and "ativo" not in h_col_names:
        db.session.execute(
            text("ALTER TABLE holiday ADD COLUMN ativo BOOLEAN NOT NULL DEFAULT 1")
        )
        db.session.commit()


def suggest_ponto_password(name: str) -> str:
    """Sugere senha de ponto: 1a letra maiuscula + posicoes alfabeticas ate 4-5 digitos.

    Ex: Luciano -> L(12)U(21) -> L1221
        Maria   -> M(13)A(1)R(18) -> M13118
    """
    letters = re.sub(r'[^a-zA-Z]', '', name)
    if not letters:
        return 'A0001'
    prefix = letters[0].upper()
    digits = ''
    for ch in letters:
        pos = ord(ch.upper()) - ord('A') + 1
        digits += str(pos)
        if len(digits) >= 4:
            break
    # caso extremo: nome muito curto com letras de posicao simples
    digits = digits[:5].ljust(4, '1')
    return prefix + digits


def parse_decimal(raw: str) -> Decimal:
    """Converte string de horas em Decimal.

    Aceita:
      - Decimal com ponto ou vírgula: "7.5", "7,5"
      - Formato H:MM (horas:minutos): "7:20" → Decimal("7.3333...")
      - Formato negativo: "-7:20", "-7.5"
    """
    if not isinstance(raw, str):
        raise ValueError("Valor de horas invalido.")
    raw = raw.strip().replace(",", ".")
    negative = raw.startswith("-")
    raw_abs = raw.lstrip("-").strip()
    if ":" in raw_abs:
        parts = raw_abs.split(":", 1)
        try:
            h = int(parts[0])
            m = int(parts[1])
        except ValueError:
            raise ValueError("Formato de horas inválido. Use H:MM ou número decimal (ex: 7:20 ou 7.33).")
        if not (0 <= m < 60):
            raise ValueError("Minutos inválidos no formato H:MM (deve ser 00–59).")
        value = Decimal(h * 60 + m) / Decimal(60)
    else:
        try:
            value = Decimal(raw_abs)
        except (InvalidOperation, AttributeError):
            raise ValueError("Valor de horas invalido.")
    return -value if negative else value


def get_setting(key: str, default: str = "") -> str:
    """Retorna o valor de uma configuracao persistida no banco."""
    s = db.session.get(Setting, key)
    return s.value if s else default


# ---------------------------------------------------------------------------
# Engine de cálculo de ponto — Regra da Cisão (Blocos 3, 4 e 5)
# ---------------------------------------------------------------------------

def _fmt_min_hhmm(minutos: int) -> str:
    """Formata minutos para H:MM (ex: 440 → '7:20')."""
    sign = "-" if minutos < 0 else ""
    minutos = abs(int(minutos))
    h, m = divmod(minutos, 60)
    return f"{sign}{h}:{m:02d}"


def is_feriado(d: date) -> bool:
    """Verifica se a data é um feriado cadastrado e ativo."""
    return db.session.query(Holiday).filter_by(holiday_date=d, ativo=True).count() > 0


def is_folga_ou_domingo(d: date) -> bool:
    """Verifica se a data é domingo ou feriado (gera direito a folga)."""
    return d.weekday() == 6 or is_feriado(d)


def _process_punches_dia(punches: list) -> dict:
    """Processa os registros de ponto do dia e retorna indicadores.

    Para registros com punch_type definido, usa lógica tipada.
    Para registros legados (punch_type=None), usa emparelhamento cronológico.

    Retorna:
        minutos: int — total de minutos trabalhados no dia
        incompleto: bool — True se houver evento de abertura sem fechamento
        intervalos: list of (time_start, time_end)
    """
    punches_sorted = sorted(punches, key=lambda p: p.punch_time)
    if not punches_sorted:
        return {"minutos": 0, "incompleto": False, "intervalos": []}

    all_legacy = all(getattr(p, "punch_type", None) is None for p in punches_sorted)
    intervalos: list[tuple] = []
    incompleto = False

    if all_legacy:
        # Emparelhamento cronológico legado: (0,1), (2,3), ...
        for i in range(0, len(punches_sorted) - 1, 2):
            t1 = punches_sorted[i].punch_time
            t2 = punches_sorted[i + 1].punch_time
            if t2 > t1:
                intervalos.append((t1, t2))
        if len(punches_sorted) % 2 != 0:
            incompleto = True
    else:
        by_type: dict[str, list] = {}
        extra_list: list = []
        for p in punches_sorted:
            pt = getattr(p, "punch_type", None) or "extra"
            if pt == "extra":
                extra_list.append(p)
            else:
                by_type.setdefault(pt, []).append(p)

        entradas = sorted(by_type.get("entrada", []), key=lambda p: p.punch_time)
        int_saidas = sorted(by_type.get("intervalo_saida", []), key=lambda p: p.punch_time)
        int_retornos = sorted(by_type.get("intervalo_retorno", []), key=lambda p: p.punch_time)
        saidas_finais = sorted(by_type.get("saida_final", []), key=lambda p: p.punch_time)

        if entradas:
            e = entradas[0]
            if int_saidas:
                s = int_saidas[0]
                intervalos.append((e.punch_time, s.punch_time))
                if int_retornos:
                    r = int_retornos[0]
                    if saidas_finais:
                        sf = saidas_finais[0]
                        intervalos.append((r.punch_time, sf.punch_time))
                    else:
                        incompleto = True
            elif saidas_finais:
                # Sem intervalo: entrada → saida_final direto
                sf = saidas_finais[0]
                intervalos.append((e.punch_time, sf.punch_time))
            else:
                incompleto = True

        # Turnos extras: emparelhamento cronológico
        for i in range(0, len(extra_list) - 1, 2):
            t1 = extra_list[i].punch_time
            t2 = extra_list[i + 1].punch_time
            if t2 > t1:
                intervalos.append((t1, t2))
        if len(extra_list) % 2 != 0:
            incompleto = True

    ref_date = date.today()
    total_min = 0
    for t_start, t_end in intervalos:
        dt_start = datetime.combine(ref_date, t_start)
        dt_end = datetime.combine(ref_date, t_end)
        diff_sec = (dt_end - dt_start).total_seconds()
        if diff_sec > 0:
            total_min += int(diff_sec / 60)

    return {"minutos": total_min, "incompleto": incompleto, "intervalos": intervalos}


# ---------------------------------------------------------------------------
# Helpers de horário agendado (schedule_json)
# ---------------------------------------------------------------------------

def _get_schedule(collab: Any) -> list[dict]:
    """Retorna lista de turnos do colaborador ou lista vazia."""
    if not getattr(collab, "schedule_json", None):
        return []
    try:
        return json.loads(collab.schedule_json).get("turnos", [])
    except Exception:
        return []


def _handle_schedule_alerts(
    collab: Any,
    punch_date: date,
    punch_type: str | None,
    data_str: str,
) -> None:
    """Agenda ou cancela lembretes com base no tipo de batida registrada."""
    if not collab.whatsapp:
        return
    turnos = _get_schedule(collab)
    if not turnos:
        return
    turno = turnos[0]  # turno principal
    GRACE = 20 * 60   # 20 minutos em segundos

    def _fire_at(hhmm: str) -> float:
        t = datetime.strptime(hhmm, "%H:%M").time()
        return datetime.combine(punch_date, t).timestamp() + GRACE

    date_iso = punch_date.isoformat()

    if punch_type == "entrada":
        # Agenda lembrete de saída para intervalo
        if turno.get("saida_intervalo"):
            key = (collab.id, date_iso, "intervalo_saida")
            with _pending_alerts_lock:
                _pending_alerts[key] = {
                    "fire_at": _fire_at(turno["saida_intervalo"]),
                    "whatsapp": collab.whatsapp,
                    "collab_name": collab.name,
                    "expected_time": turno["saida_intervalo"],
                    "tipo": "intervalo_saida",
                    "data_str": data_str,
                }

    elif punch_type == "intervalo_saida":
        # Cancela lembrete de intervalo (foi registrado a tempo)
        with _pending_alerts_lock:
            _pending_alerts.pop((collab.id, date_iso, "intervalo_saida"), None)

    elif punch_type == "intervalo_retorno":
        # Cancela lembrete de intervalo residual e agenda lembrete de saída final
        with _pending_alerts_lock:
            _pending_alerts.pop((collab.id, date_iso, "intervalo_saida"), None)
        if turno.get("saida_final"):
            key = (collab.id, date_iso, "saida_final")
            with _pending_alerts_lock:
                _pending_alerts[key] = {
                    "fire_at": _fire_at(turno["saida_final"]),
                    "whatsapp": collab.whatsapp,
                    "collab_name": collab.name,
                    "expected_time": turno["saida_final"],
                    "tipo": "saida_final",
                    "data_str": data_str,
                }

    elif punch_type == "saida_final":
        # Cancela lembrete de saída final (foi registrado a tempo)
        with _pending_alerts_lock:
            _pending_alerts.pop((collab.id, date_iso, "saida_final"), None)


def _calc_ponto_indicadores(
    collab_id: int,
    year: int | None = None,
    month: int | None = None,
) -> dict:
    """Calcula indicadores dinâmicos de ponto — Regra da Cisão.

    Se year/month forem None, calcula sobre TODOS os registros (saldo acumulado).
    Retorna dict com todos os indicadores necessários para o painel.
    """
    if year is not None and month is not None:
        month_start: date | None = date(year, month, 1)
        month_end: date | None = (
            date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
        )
    else:
        month_start = month_end = None

    q = PunchRecord.query.filter(PunchRecord.collaborator_id == collab_id)
    if month_start and month_end:
        q = q.filter(
            PunchRecord.punch_date >= month_start,
            PunchRecord.punch_date < month_end,
        )
    punches = q.order_by(PunchRecord.punch_date.asc(), PunchRecord.punch_time.asc()).all()

    days_punches: dict[date, list] = {}
    for p in punches:
        days_punches.setdefault(p.punch_date, []).append(p)

    h_normais_min = 0
    folga_bruto_min = 0
    extra_acumulado_min = 0
    h_bruto_min = 0
    dias_incompletos: list[date] = []
    dias_processados = 0

    for d, day_punches in sorted(days_punches.items()):
        result = _process_punches_dia(day_punches)
        if result["incompleto"]:
            dias_incompletos.append(d)
            continue
        worked_min = result["minutos"]
        if worked_min == 0:
            continue
        h_bruto_min += worked_min
        dias_processados += 1
        normal_today = min(worked_min, JORNADA_MIN)
        # Dia é folga se: domingo, feriado cadastrado, ou qualquer batida do dia marcada como folga
        dia_e_folga = (
            is_folga_ou_domingo(d)
            or any(getattr(p, "gives_folga", False) for p in day_punches)
        )
        if dia_e_folga:
            folga_bruto_min += normal_today
        else:
            h_normais_min += normal_today
        if worked_min > JORNADA_MIN:
            extra_acumulado_min += worked_min - JORNADA_MIN

    ajustes = PontoAjuste.query.filter_by(collaborator_id=collab_id).all()
    desconto_min = sum(a.minutos for a in ajustes if a.tipo == "desconto_extra")
    folgas_usadas_count = sum(1 for a in ajustes if a.tipo == "uso_folga")

    extra_saldo_min = max(0, extra_acumulado_min - desconto_min)

    # Créditos manuais de folga via HourEntry (admin: grant_folga)
    q_creditos = HourEntry.query.filter(
        HourEntry.collaborator_id == collab_id,
        HourEntry.gives_folga == True,
    )
    if month_start and month_end:
        q_creditos = q_creditos.filter(
            HourEntry.entry_date >= month_start,
            HourEntry.entry_date < month_end,
        )
    folga_bruto_min += q_creditos.count() * JORNADA_MIN

    folga_bruto_saldo_min = max(0, folga_bruto_min - folgas_usadas_count * JORNADA_MIN)

    collab = db.session.get(Collaborator, collab_id)
    global_rate = Decimal(get_setting("daily_rate", "0"))
    daily_rate = (
        Decimal(collab.daily_rate)
        if collab and collab.daily_rate is not None
        else global_rate
    )
    rate_por_min = daily_rate / Decimal(JORNADA_MIN) if daily_rate > 0 else Decimal("0")
    r_extra_valor = rate_por_min * Decimal(extra_saldo_min)

    return {
        "h_normais_min": h_normais_min,
        "folga_bruto_min": folga_bruto_min,
        "folga_bruto_saldo_min": folga_bruto_saldo_min,
        "folga_bruto_dias": folga_bruto_saldo_min / JORNADA_MIN,
        "extra_acumulado_min": extra_acumulado_min,
        "extra_saldo_min": extra_saldo_min,
        "h_bruto_min": h_bruto_min,
        "dias_incompletos": [d.isoformat() for d in dias_incompletos],
        "dias_processados": dias_processados,
        "rate_por_min": rate_por_min,
        "r_extra_valor": r_extra_valor,
        "daily_rate": daily_rate,
        "desconto_min": desconto_min,
        "folgas_usadas_count": folgas_usadas_count,
    }


def _calc_meta_mensal(year: int, month: int) -> int:
    """Calcula a meta de horas do mês em minutos (exclui domingos e feriados)."""
    _, days_in_month = calendar.monthrange(year, month)
    meta_min = 0
    for day in range(1, days_in_month + 1):
        d = date(year, month, day)
        if d.weekday() != 6 and not is_feriado(d):
            meta_min += JORNADA_MIN
    return meta_min


def _calc_meta_semanal(iso_year: int, iso_week: int) -> int:
    """Calcula a meta de horas da semana (seg-sab) em minutos, excluindo feriados cadastrados."""
    meta_min = 0
    for weekday in range(1, 7):  # 1=segunda, 6=sábado
        d = date.fromisocalendar(iso_year, iso_week, weekday)
        if not is_feriado(d):
            meta_min += JORNADA_MIN
    return meta_min


def monthly_summary(
    year: int | None = None,
    month: int | None = None,
) -> tuple[list[dict], dict]:
    """Monta o resumo mensal agregado por colaborador."""
    today = date.today()
    year = year or today.year
    month = month or today.month
    month_start = date(year, month, 1)
    # first day of next month as upper bound
    if month == 12:
        month_end = date(year + 1, 1, 1)
    else:
        month_end = date(year, month + 1, 1)

    entries = (
        db.session.query(HourEntry, Collaborator)
        .join(Collaborator, Collaborator.id == HourEntry.collaborator_id)
        .filter(
            HourEntry.entry_date >= month_start,
            HourEntry.entry_date < month_end,
            HourEntry.archived == False,  # noqa: E712
        )
        .order_by(HourEntry.entry_date.desc(), HourEntry.id.desc())
        .all()
    )

    grouped: dict[int, dict] = defaultdict(
        lambda: {
            "name": "",
            "role": "",
            "collab_id": 0,
            "positive": Decimal("0"),
            "negative": Decimal("0"),
            "net": Decimal("0"),
            "days": 0,
            "folga_days": 0,
        }
    )

    for entry, collab in entries:
        bucket = grouped[collab.id]
        bucket["name"] = collab.name
        bucket["role"] = collab.role or "-"
        bucket["collab_id"] = collab.id
        bucket["folga_days"] = collab.folga_days
        bucket["active"] = collab.active
        bucket["daily_rate"] = collab.daily_rate
        bucket["ponto_password_hash"] = collab.ponto_password_hash
        hours_value = Decimal(entry.hours)
        if hours_value >= 0:
            bucket["positive"] += hours_value
        else:
            bucket["negative"] += abs(hours_value)
        bucket["net"] = bucket["positive"] - bucket["negative"]
        bucket["days"] = int(max(bucket["net"], Decimal("0")) * 60 // 440)

    # Ensure all collaborators appear even with no entries this month
    all_collabs = Collaborator.query.order_by(Collaborator.active.desc(), Collaborator.name.asc()).all()
    for collab in all_collabs:
        if collab.id not in grouped:
            grouped[collab.id] = {
                "name": collab.name,
                "role": collab.role or "-",
                "collab_id": collab.id,
                "positive": Decimal("0"),
                "negative": Decimal("0"),
                "net": Decimal("0"),
                "days": 0,
                "folga_days": collab.folga_days,
                "active": collab.active,
                "daily_rate": collab.daily_rate,
                "ponto_password_hash": collab.ponto_password_hash,
            }
        else:
            # fill fields that might be missing if collab appeared only via entries
            grouped[collab.id].setdefault("active", collab.active)
            grouped[collab.id].setdefault("daily_rate", collab.daily_rate)
            grouped[collab.id].setdefault("ponto_password_hash", collab.ponto_password_hash)

    cards = sorted(grouped.values(), key=lambda item: (not item["active"], item["name"].lower()))

    total_positive = sum((c["positive"] for c in cards), Decimal("0"))
    total_negative = sum((c["negative"] for c in cards), Decimal("0"))
    total_net = sum((c["net"] for c in cards), Decimal("0"))
    total_days = sum(c["days"] for c in cards)

    # navigation helpers
    prev_month = month - 1 if month > 1 else 12
    prev_year = year if month > 1 else year - 1
    next_month = month + 1 if month < 12 else 1
    next_year = year if month < 12 else year + 1

    totals = {
        "positive": total_positive,
        "negative": total_negative,
        "net": total_net,
        "days": total_days,
        "month_label": f"{month:02d}/{year}",
        "year": year,
        "month": month,
        "prev_param": f"{prev_year}-{prev_month:02d}",
        "next_param": f"{next_year}-{next_month:02d}",
        "month_start": month_start.isoformat(),
    }

    return cards, totals


def _parse_month_param(raw: str | None) -> tuple[int, int]:
    """Parse 'YYYY-MM' query param, fallback to current month."""
    today = date.today()
    if raw:
        try:
            parts = raw.split("-")
            return int(parts[0]), int(parts[1])
        except (ValueError, IndexError):
            pass
    return today.year, today.month


@app.get("/")
def index():
    """Renderiza o painel principal com resumo e lancamentos do mes."""
    year, month = _parse_month_param(request.args.get("month"))
    collaborators = Collaborator.query.order_by(
        Collaborator.active.desc(),
        Collaborator.name.asc(),
    ).all()
    # Últimas 5 entradas (independente do mês selecionado) para o painel inicial
    entries = (
        HourEntry.query.join(
            Collaborator,
            Collaborator.id == HourEntry.collaborator_id,
        )
        .filter(HourEntry.archived == False)  # noqa: E712
        .order_by(HourEntry.entry_date.desc(), HourEntry.id.desc())
        .limit(5)
        .all()
    )
    # Batidas do dia atual sem par (processado=False) — aparecem como "Em andamento"
    today_date = date.today()
    pending_punches = (
        PunchRecord.query
        .join(Collaborator, Collaborator.id == PunchRecord.collaborator_id)
        .filter(
            PunchRecord.punch_date == today_date,
            PunchRecord.processed == False,  # noqa: E712
        )
        .order_by(PunchRecord.punch_time.desc())
        .all()
    )
    cards, totals = monthly_summary(year, month)
    daily_rate = Decimal(get_setting("daily_rate", "0"))
    total_value = (
        Decimal(totals["days"]) * daily_rate
        if daily_rate > 0
        else Decimal("0")
    )
    return render_template(
        "index.html",
        collaborators=collaborators,
        entries=entries,
        pending_punches=pending_punches,
        cards=cards,
        totals=totals,
        daily_rate=daily_rate,
        total_value=total_value,
    )


@app.get("/login")
def login():
    """Exibe a pagina de login para usuarios nao autenticados."""
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    return render_template("login.html")


@app.post("/login")
def login_post():
    """Autentica o usuario administrador e inicia a sessao."""
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""

    user = User.query.filter(
        func.lower(User.username) == username.lower()
    ).first()
    if not user or not check_password_hash(user.password_hash, password):
        flash("Usuario ou senha invalidos.", "danger")
        return redirect(url_for("login"))

    login_user(user)
    flash("Login efetuado com sucesso.", "success")
    return redirect(url_for("index"))


@app.route("/logout", methods=["GET", "POST"])
@login_required
def logout():
    """Encerra a sessao do usuario autenticado."""
    logout_user()
    flash("Sessao encerrada.", "info")
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# Gestao de admins
# ---------------------------------------------------------------------------

@app.get("/settings/admins")
@login_required
def admins_list():
    """Lista todos os usuarios administradores."""
    admins = User.query.order_by(User.username.asc()).all()
    return render_template("admins.html", admins=admins)


@app.route("/settings/admins/create", methods=["GET", "POST"])
@login_required
def admin_create():
    """Cria um novo usuario administrador."""
    if request.method == "GET":
        return redirect(url_for("admins_list"))
    username = (request.form.get("username") or "").strip()
    password = (request.form.get("password") or "").strip()
    if not username or not password:
        flash("Nome de usuario e senha sao obrigatorios.", "warning")
        return redirect(url_for("admins_list"))
    if User.query.filter(func.lower(User.username) == username.lower()).first():
        flash(f"Usuario '{username}' ja existe.", "warning")
        return redirect(url_for("admins_list"))
    db.session.add(User(
        username=username,
        password_hash=generate_password_hash(password),
    ))
    db.session.commit()
    flash(f"Admin '{username}' criado com sucesso.", "success")
    return redirect(url_for("admins_list"))


@app.route("/settings/admins/<int:user_id>/delete", methods=["GET", "POST"])
@login_required
def admin_delete(user_id: int):
    """Remove um usuario administrador (nao pode remover a si mesmo)."""
    if request.method == "GET":
        return redirect(url_for("admins_list"))
    if user_id == current_user.id:
        flash("Voce nao pode remover sua propria conta.", "danger")
        return redirect(url_for("admins_list"))
    target = db.session.get(User, user_id)
    if not target:
        flash("Usuario nao encontrado.", "warning")
        return redirect(url_for("admins_list"))
    db.session.delete(target)
    db.session.commit()
    flash(f"Admin '{target.username}' removido.", "success")
    return redirect(url_for("admins_list"))


@app.route("/settings/daily-rate", methods=["GET", "POST"])
@login_required
def set_daily_rate():
    """Persiste o valor da diaria (7h20) usado para calculo de custo."""
    if request.method == "GET":
        return redirect(url_for("index"))
    raw = (request.form.get("daily_rate") or "").strip().replace(",", ".")
    month_param = request.form.get("month", "")
    try:
        rate = Decimal(raw)
        if rate < 0:
            raise ValueError
        s = db.session.get(Setting, "daily_rate")
        if s:
            s.value = str(rate)
        else:
            db.session.add(Setting(key="daily_rate", value=str(rate)))
        db.session.commit()
        flash("Valor da diaria atualizado.", "success")
    except (InvalidOperation, ValueError):
        flash("Informe um valor numerico valido.", "danger")
    return redirect(url_for("index", month=month_param))


@app.route("/collaborators", methods=["GET", "POST"])
@login_required
def create_collaborator():
    """Cria um novo colaborador ativo."""
    if request.method == "GET":
        return redirect(url_for("index"))
    name = (request.form.get("name") or "").strip()
    role = (request.form.get("role") or "").strip()
    daily_rate_raw = (request.form.get("daily_rate") or "").strip()
    ponto_pass = (request.form.get("ponto_password") or "").strip()

    if not name:
        flash("Nome do colaborador e obrigatorio.", "warning")
        return redirect(url_for("index"))

    daily_rate: Decimal | None = None
    if daily_rate_raw:
        try:
            daily_rate = parse_decimal(daily_rate_raw)
            if daily_rate < 0:
                raise ValueError
        except ValueError:
            flash("Informe uma diaria valida para o colaborador.", "warning")
            return redirect(url_for("index"))

    db.session.add(
        Collaborator(
            name=name,
            role=role or None,
            daily_rate=daily_rate,
            active=True,
            ponto_password_hash=(
                generate_password_hash(ponto_pass)
                if ponto_pass else None
            ),
        )
    )
    db.session.commit()
    wz.colaborador_criado(name, role or None, daily_rate)
    flash("Colaborador criado com sucesso.", "success")
    return redirect(url_for("index"))


@app.route("/collaborators/<int:collaborator_id>/toggle", methods=["GET", "POST"])
@login_required
def toggle_collaborator(collaborator_id: int):
    """Ativa ou desativa um colaborador existente."""
    if request.method == "GET":
        return redirect(url_for("index"))
    collaborator = db.session.get(Collaborator, collaborator_id)
    if not collaborator:
        flash("Colaborador nao encontrado.", "danger")
        return redirect(url_for("index"))

    collaborator.active = not collaborator.active
    db.session.commit()
    wz.colaborador_toggle(collaborator.name, collaborator.active)
    flash("Status do colaborador atualizado.", "success")
    return redirect(url_for("index"))


@app.route("/collaborators/<int:collaborator_id>/update", methods=["GET", "POST"])
@login_required
def update_collaborator(collaborator_id: int):
    if request.method == "GET":
        return redirect(url_for("collaborator_history", collaborator_id=collaborator_id))
    """Atualiza nome e funcao de um colaborador existente."""
    from_history = request.form.get("_from_history")

    def _redirect_target():
        if from_history:
            return redirect(
                url_for(
                    "collaborator_history",
                    collaborator_id=collaborator_id,
                )
            )
        return redirect(url_for("index"))

    collaborator = db.session.get(Collaborator, collaborator_id)
    if not collaborator:
        flash("Colaborador nao encontrado.", "danger")
        return _redirect_target()

    name = (request.form.get("name") or "").strip()
    role = (request.form.get("role") or "").strip()
    daily_rate_raw = (request.form.get("daily_rate") or "").strip()

    if not name:
        flash("Nome do colaborador e obrigatorio.", "warning")
        return _redirect_target()

    daily_rate: Decimal | None = None
    if daily_rate_raw:
        try:
            daily_rate = parse_decimal(daily_rate_raw)
            if daily_rate < 0:
                raise ValueError
        except ValueError:
            flash("Informe uma diaria valida para o colaborador.", "warning")
            return _redirect_target()

    old_name = collaborator.name
    old_role = collaborator.role
    old_daily_rate = collaborator.daily_rate
    collaborator.name = name
    collaborator.role = role or None
    collaborator.daily_rate = daily_rate
    db.session.commit()

    wz.colaborador_atualizado(
        old_name,
        collaborator.name,
        old_role,
        collaborator.role,
        old_daily_rate,
        collaborator.daily_rate,
    )
    flash("Colaborador atualizado com sucesso.", "success")
    return _redirect_target()


@app.route("/entries", methods=["GET", "POST"])
@login_required
def create_entry():
    """Registra um novo lancamento de horas para um colaborador."""
    if request.method == "GET":
        return redirect(url_for("index"))
    collaborator_id_raw = request.form.get("collaborator_id") or ""
    entry_date_raw = request.form.get("entry_date") or ""
    hours_raw = request.form.get("hours") or ""
    note = (request.form.get("note") or "").strip()
    gives_folga = bool(request.form.get("gives_folga"))

    try:
        collaborator_id = int(collaborator_id_raw)
        entry_date = datetime.strptime(entry_date_raw, "%Y-%m-%d").date()
        hours = parse_decimal(hours_raw)
    except ValueError as exc:
        flash(str(exc), "warning")
        return redirect(url_for("index"))

    collaborator = db.session.get(Collaborator, collaborator_id)
    if not collaborator:
        flash("Colaborador invalido.", "danger")
        return redirect(url_for("index"))

    db.session.add(
        HourEntry(
            collaborator_id=collaborator_id,
            entry_date=entry_date,
            hours=hours,
            note=note or None,
            gives_folga=gives_folga,
        )
    )
    if gives_folga:
        collaborator.folga_days += 1
    db.session.commit()
    wz.entry_criado(
        collaborator.name,
        collaborator.role or "-",
        entry_date.strftime("%d/%m/%Y"),
        hours,
        note or None,
    )
    flash("Lancamento registrado.", "success")
    return redirect(url_for("index"))


@app.route("/entries/<int:entry_id>/update", methods=["GET", "POST"])
@login_required
def update_entry(entry_id: int):
    """Atualiza data, horas e observacao de um lancamento."""
    if request.method == "GET":
        return redirect(url_for("index"))
    entry = db.session.get(HourEntry, entry_id)
    if not entry:
        flash("Lancamento nao encontrado.", "danger")
        return redirect(url_for("index"))

    try:
        entry.entry_date = datetime.strptime(
            request.form.get("entry_date") or "",
            "%Y-%m-%d",
        ).date()
        entry.hours = parse_decimal(request.form.get("hours") or "")
    except ValueError as exc:
        flash(str(exc), "warning")
        return redirect(url_for("index"))

    new_gives_folga = bool(request.form.get("gives_folga"))
    if new_gives_folga != entry.gives_folga:
        collab = db.session.get(Collaborator, entry.collaborator_id)
        if collab:
            if new_gives_folga:
                collab.folga_days += 1
            else:
                collab.folga_days = max(0, collab.folga_days - 1)
    entry.gives_folga = new_gives_folga
    entry.note = (request.form.get("note") or "").strip() or None
    entry.updated_at = datetime.utcnow()
    db.session.commit()
    wz.entry_atualizado(
        entry.collaborator.name,
        entry.entry_date.strftime("%d/%m/%Y"),
        entry.hours,
        entry.note,
    )
    flash("Lancamento atualizado.", "success")
    from_history = request.form.get("_from_history")
    if from_history:
        return redirect(
            url_for("collaborator_history", collaborator_id=from_history)
        )
    return redirect(url_for("index"))


@app.route("/entries/<int:entry_id>/delete", methods=["GET", "POST"])
@login_required
def delete_entry(entry_id: int):
    """Remove um lancamento existente."""
    if request.method == "GET":
        return redirect(url_for("index"))
    entry = db.session.get(HourEntry, entry_id)
    if not entry:
        flash("Lancamento nao encontrado.", "danger")
        return redirect(url_for("index"))

    collab_name = entry.collaborator.name
    date_str = entry.entry_date.strftime("%d/%m/%Y")
    hours_val = entry.hours
    from_history = request.form.get("_from_history")
    collab_id_hist = entry.collaborator_id
    if entry.gives_folga:
        collab = db.session.get(Collaborator, entry.collaborator_id)
        if collab:
            collab.folga_days = max(0, collab.folga_days - 1)
    db.session.delete(entry)
    db.session.commit()
    wz.entry_removido(collab_name, date_str, hours_val)
    flash("Lancamento removido.", "info")
    if from_history:
        return redirect(
            url_for("collaborator_history", collaborator_id=collab_id_hist)
        )
    return redirect(url_for("index"))


@app.route("/collaborators/<int:collaborator_id>/use-folga", methods=["GET", "POST"])
@login_required
def use_folga(collaborator_id: int):
    """Desconta 1 dia de folga do colaborador e cria lancamento negativo de 7h20."""
    if request.method == "GET":
        return redirect(url_for("collaborator_history", collaborator_id=collaborator_id))
    collab = db.session.get(Collaborator, collaborator_id)
    if not collab:
        flash("Colaborador nao encontrado.", "danger")
        return redirect(url_for("index"))

    if collab.folga_days < 1:
        flash(f"{collab.name} nao possui dias de folga disponíveis.", "warning")
        from_history = request.form.get("_from_history")
        if from_history:
            return redirect(url_for("collaborator_history", collaborator_id=collaborator_id))
        return redirect(url_for("index"))

    date_raw = (request.form.get("folga_date") or "").strip()
    try:
        folga_date = datetime.strptime(date_raw, "%Y-%m-%d").date()
    except ValueError:
        folga_date = date.today()

    note = (request.form.get("note") or "Folga utilizada").strip()

    db.session.add(HourEntry(
        collaborator_id=collaborator_id,
        entry_date=folga_date,
        hours=Decimal("-7.33"),
        note=note,
        gives_folga=False,
    ))
    db.session.add(PontoAjuste(
        collaborator_id=collaborator_id,
        tipo="uso_folga",
        minutos=JORNADA_MIN,
        data_referencia=folga_date,
        obs=note,
        criado_por="admin",
    ))
    collab.folga_days -= 1
    db.session.commit()
    flash(
        f"Folga descontada de {collab.name}. "
        f"Saldo restante: {collab.folga_days} dia(s).",
        "success",
    )
    from_history = request.form.get("_from_history")
    if from_history:
        return redirect(url_for("collaborator_history", collaborator_id=collaborator_id))
    return redirect(url_for("index"))


@app.route("/collaborators/<int:collaborator_id>/grant-folga", methods=["GET", "POST"])
@login_required
def grant_folga(collaborator_id: int):
    """Credita 1 dia de folga manualmente (apenas admin), sem exigir comprovante."""
    if request.method == "GET":
        return redirect(url_for("collaborator_history", collaborator_id=collaborator_id))
    collab = db.session.get(Collaborator, collaborator_id)
    if not collab:
        flash("Colaborador nao encontrado.", "danger")
        return redirect(url_for("index"))

    justificativa = (request.form.get("justificativa") or "").strip()
    if not justificativa:
        flash("Justificativa obrigatoria para concessao manual de folga.", "warning")
        return redirect(url_for("collaborator_history", collaborator_id=collaborator_id))

    date_raw = (request.form.get("grant_date") or "").strip()
    try:
        grant_date = datetime.strptime(date_raw, "%Y-%m-%d").date()
    except ValueError:
        grant_date = date.today()

    note = f"Crédito manual (admin): {justificativa}"
    db.session.add(
        HourEntry(
            collaborator_id=collaborator_id,
            entry_date=grant_date,
            hours=Decimal("7.33"),
            note=note,
            gives_folga=True,
        )
    )
    collab.folga_days += 1
    db.session.commit()
    flash(
        f"1 dia de folga creditado a {collab.name}. "
        f"Saldo atual: {collab.folga_days} dia(s).",
        "success",
    )
    return redirect(url_for("collaborator_history", collaborator_id=collaborator_id))


@app.get("/collaborators/<int:collaborator_id>/history")
def collaborator_history(collaborator_id: int):
    """Exibe o historico completo de lancamentos de um colaborador."""
    collab = db.session.get(Collaborator, collaborator_id)
    if not collab:
        flash("Colaborador nao encontrado.", "danger")
        return redirect(url_for("index"))

    # Controle de acesso: colaborador ponto só pode ver o próprio histórico
    sess_id = _flask_session.get("ponto_collab_id")
    if not current_user.is_authenticated and sess_id and sess_id != collaborator_id:
        flash("Você só pode acessar o seu próprio histórico.", "warning")
        return redirect(url_for("collaborator_history", collaborator_id=sess_id))

    # Indica se quem acessa é o dono do perfil ou admin
    is_own_profile = current_user.is_authenticated or (sess_id == collaborator_id)

    all_entries = (
        HourEntry.query
        .filter_by(collaborator_id=collaborator_id)
        .filter(HourEntry.archived == False)  # noqa: E712
        .order_by(HourEntry.entry_date.desc(), HourEntry.id.desc())
        .all()
    )

    # agrupar por mes (ano-mes)
    months_dict: dict[str, list] = defaultdict(list)
    for e in all_entries:
        key = f"{e.entry_date.year}-{e.entry_date.month:02d}"
        months_dict[key].append(e)
    all_month_keys = sorted(months_dict.keys(), reverse=True)

    # Mes selecionado (paginacao de mes via ?month=YYYY-MM)
    sel_month_key = request.args.get("month", "")
    if not sel_month_key or sel_month_key not in months_dict:
        sel_month_key = all_month_keys[0] if all_month_keys else None

    if sel_month_key and sel_month_key in all_month_keys:
        m_idx = all_month_keys.index(sel_month_key)
        prev_month_key = all_month_keys[m_idx + 1] if m_idx + 1 < len(all_month_keys) else None
        next_month_key = all_month_keys[m_idx - 1] if m_idx > 0 else None
        y, mo = int(sel_month_key.split("-")[0]), int(sel_month_key.split("-")[1])
        sel_month_label = f"{MESES_PT[mo]} {y}"
    else:
        prev_month_key = next_month_key = None
        sel_month_label = ""

    # Semanas dentro do mes selecionado (agrupadas por semana ISO, desc)
    month_entries_all = months_dict.get(sel_month_key, []) if sel_month_key else []
    weeks: list[dict] = []
    if month_entries_all:
        week_groups: dict = {}
        for e in month_entries_all:
            iso_year, iso_week, _ = e.entry_date.isocalendar()
            wk_key = (iso_year, iso_week)
            week_groups.setdefault(wk_key, []).append(e)
        for wk_key in sorted(week_groups.keys(), reverse=True):
            wk_entries = week_groups[wk_key]
            dates = [e.entry_date for e in wk_entries]
            d_min, d_max = min(dates), max(dates)
            weeks.append({
                "entries": wk_entries,
                "label": f"{d_min.strftime('%d/%m')} – {d_max.strftime('%d/%m')}",
                "wk_key": wk_key,
            })

    sel_week_idx = 0
    try:
        sel_week_idx = int(request.args.get("week", 0))
    except (ValueError, TypeError):
        pass
    sel_week_idx = max(0, min(sel_week_idx, len(weeks) - 1)) if weeks else 0
    week_data = weeks[sel_week_idx] if weeks else {"entries": [], "label": ""}
    prev_week = sel_week_idx + 1 if sel_week_idx + 1 < len(weeks) else None
    next_week = sel_week_idx - 1 if sel_week_idx > 0 else None

    # Totais do mes inteiro (para exibir no header do mes)
    month_pos = Decimal("0")
    month_neg = Decimal("0")
    for e in month_entries_all:
        v = Decimal(e.hours)
        if v >= 0:
            month_pos += v
        else:
            month_neg += abs(v)

    # Totais globais (hero) — filtrados pelo ?sel= se presente
    sel_param = request.args.get("sel", "")
    if sel_param:
        sel_keys = set(sel_param.split(",")) & set(all_month_keys)
        if not sel_keys:
            sel_keys = set(all_month_keys)
    else:
        sel_keys = set(all_month_keys)
    sel_is_all = (sel_keys == set(all_month_keys))

    total_pos = Decimal("0")
    total_neg = Decimal("0")
    total_count = 0
    for e in all_entries:
        key = f"{e.entry_date.year}-{e.entry_date.month:02d}"
        if key not in sel_keys:
            continue
        v = Decimal(e.hours)
        total_count += 1
        if v >= 0:
            total_pos += v
        else:
            total_neg += abs(v)
    total_net = total_pos - total_neg
    total_days = int(max(total_net, Decimal("0")) * 60 // 440)

    totals_global = {
        "positive": total_pos,
        "negative": total_neg,
        "net": total_net,
        "days": total_days,
        "count": total_count,
    }

    global_daily_rate = Decimal(get_setting("daily_rate", "0"))
    collab_daily_rate = (
        Decimal(collab.daily_rate)
        if collab.daily_rate is not None
        else global_daily_rate
    )
    total_value = (
        Decimal(total_days) * collab_daily_rate
        if collab_daily_rate > 0
        else Decimal("0")
    )

    # ── Indicadores de ponto (Regra da Cisão) para o mês selecionado ──
    ponto_year  = int(sel_month_key[:4])  if sel_month_key else date.today().year
    ponto_month = int(sel_month_key[5:7]) if sel_month_key else date.today().month

    ind_mes   = _calc_ponto_indicadores(collaborator_id, ponto_year, ponto_month)
    ind_total = _calc_ponto_indicadores(collaborator_id)

    # Meta e faltantes da semana selecionada (seg-sab, excluindo feriados)
    if week_data.get("wk_key"):
        _wk_iso_year, _wk_iso_week = week_data["wk_key"]
        meta_semana_min = _calc_meta_semanal(_wk_iso_year, _wk_iso_week)
        _wk_monday   = date.fromisocalendar(_wk_iso_year, _wk_iso_week, 1)
        _wk_saturday = date.fromisocalendar(_wk_iso_year, _wk_iso_week, 6)
        _wk_punches = (
            PunchRecord.query
            .filter(
                PunchRecord.collaborator_id == collaborator_id,
                PunchRecord.punch_date >= _wk_monday,
                PunchRecord.punch_date <= _wk_saturday,
            )
            .order_by(PunchRecord.punch_date.asc(), PunchRecord.punch_time.asc())
            .all()
        )
        _wk_days: dict[date, list] = {}
        for _p in _wk_punches:
            _wk_days.setdefault(_p.punch_date, []).append(_p)
        _wk_worked_min = 0
        for _wd, _wdp in _wk_days.items():
            _wr = _process_punches_dia(_wdp)
            if not _wr["incompleto"]:
                _wk_worked_min += min(_wr["minutos"], JORNADA_MIN)
        # Folgas usadas na semana em dias úteis reduzem a meta efetiva
        _wk_folgas = PontoAjuste.query.filter(
            PontoAjuste.collaborator_id == collaborator_id,
            PontoAjuste.tipo == "uso_folga",
            PontoAjuste.data_referencia >= _wk_monday,
            PontoAjuste.data_referencia <= _wk_saturday,
        ).all()
        _wk_folgas_uteis_min = sum(
            JORNADA_MIN for a in _wk_folgas
            if a.data_referencia and not is_folga_ou_domingo(a.data_referencia)
        )
        # Folgas na semana reduzem a própria meta (não apenas os faltantes)
        meta_semana_min = max(0, meta_semana_min - _wk_folgas_uteis_min)
        faltantes_semana_min = max(0, meta_semana_min - _wk_worked_min)
    else:
        meta_semana_min = 0
        faltantes_semana_min = 0

    _ms = date(ponto_year, ponto_month, 1)
    _me = date(ponto_year + 1, 1, 1) if ponto_month == 12 else date(ponto_year, ponto_month + 1, 1)
    feriados_mes = Holiday.query.filter(
        Holiday.holiday_date >= _ms,
        Holiday.holiday_date <  _me,
    ).order_by(Holiday.holiday_date.asc()).all()

    ajustes = (
        PontoAjuste.query.filter_by(collaborator_id=collaborator_id)
        .order_by(PontoAjuste.created_at.desc())
        .limit(20)
        .all()
    )
    sched_turnos = _get_schedule(collab)

    return render_template(
        "collab_history.html",
        collab=collab,
        totals=totals_global,
        collab_daily_rate=collab_daily_rate,
        total_value=total_value,
        today=date.today().isoformat(),
        all_month_keys=all_month_keys,
        sel_keys=sel_keys,
        sel_is_all=sel_is_all,
        sel_month_key=sel_month_key,
        sel_month_label=sel_month_label,
        prev_month_key=prev_month_key,
        next_month_key=next_month_key,
        weeks=weeks,
        week_data=week_data,
        sel_week_idx=sel_week_idx,
        prev_week=prev_week,
        next_week=next_week,
        month_pos=month_pos,
        month_neg=month_neg,
        # ponto indicators
        ind_mes=ind_mes,
        ind_total=ind_total,
        meta_semana_min=meta_semana_min,
        faltantes_semana_min=faltantes_semana_min,
        feriados_mes=feriados_mes,
        ajustes=ajustes,
        sched_turnos=sched_turnos,
        fmt_min=_fmt_min_hhmm,
        JORNADA_MIN=JORNADA_MIN,
        ponto_year=ponto_year,
        ponto_month=ponto_month,
        is_own_profile=is_own_profile,
    )


# ---------------------------------------------------------------------------
# Arquivo morto
# ---------------------------------------------------------------------------

@app.route("/archive/month", methods=["GET", "POST"])
@login_required
def archive_month():
    if request.method == "GET":
        return redirect(url_for("index"))
    """Arquiva todos os lançamentos de um mês (remove do painel ativo)."""
    year, month = _parse_month_param(request.form.get("month"))
    month_start = date(year, month, 1)
    month_end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    count = HourEntry.query.filter(
        HourEntry.entry_date >= month_start,
        HourEntry.entry_date < month_end,
        HourEntry.archived == False,  # noqa: E712
    ).update({"archived": True})
    db.session.commit()
    flash(f"{count} lançamentos de {month:02d}/{year} arquivados.", "success")
    return redirect(url_for("index"))


@app.route("/archive/month/restore", methods=["GET", "POST"])
@login_required
def restore_month():
    if request.method == "GET":
        return redirect(url_for("index"))
    """Restaura lançamentos arquivados de um mês para o painel ativo."""
    year, month = _parse_month_param(request.form.get("month"))
    month_start = date(year, month, 1)
    month_end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    count = HourEntry.query.filter(
        HourEntry.entry_date >= month_start,
        HourEntry.entry_date < month_end,
        HourEntry.archived == True,  # noqa: E712
    ).update({"archived": False})
    db.session.commit()
    flash(f"{count} lançamentos de {month:02d}/{year} restaurados.", "success")
    return redirect(url_for("archive_view"))


@app.get("/archive")
@login_required
def archive_view():
    """Exibe o arquivo morto — meses arquivados com resumo por colaborador."""
    # Agrupa meses que têm ao menos um lançamento arquivado
    archived_entries = (
        db.session.query(HourEntry, Collaborator)
        .join(Collaborator, Collaborator.id == HourEntry.collaborator_id)
        .filter(HourEntry.archived == True)  # noqa: E712
        .order_by(HourEntry.entry_date.desc())
        .all()
    )

    # Agrupa por ano-mês
    months_dict: dict[str, dict] = defaultdict(lambda: {
        "label": "", "key": "", "collabs": defaultdict(lambda: {
            "name": "", "positive": Decimal("0"), "negative": Decimal("0"), "net": Decimal("0"),
        })
    })
    for entry, collab in archived_entries:
        key = f"{entry.entry_date.year}-{entry.entry_date.month:02d}"
        bucket = months_dict[key]
        bucket["label"] = f"{_MESES_PT[entry.entry_date.month]} {entry.entry_date.year}"
        bucket["key"] = key
        cb = bucket["collabs"][collab.id]
        cb["name"] = collab.name
        h = Decimal(entry.hours)
        if h >= 0:
            cb["positive"] += h
        else:
            cb["negative"] += abs(h)
        cb["net"] = cb["positive"] - cb["negative"]

    months = sorted(months_dict.values(), key=lambda m: m["key"], reverse=True)
    for m in months:
        m["collabs"] = sorted(m["collabs"].values(), key=lambda c: c["name"].lower())

    return render_template("archive.html", months=months)


# ---------------------------------------------------------------------------
# PDF individual do colaborador
# ---------------------------------------------------------------------------

@app.get("/collaborators/<int:collaborator_id>/pdf")
def collaborator_pdf(collaborator_id: int):
    """Gera PDF com resumo e histórico completo do colaborador no mês."""
    year, month = _parse_month_param(request.args.get("month"))
    collab = db.session.get(Collaborator, collaborator_id)
    if not collab:
        flash("Colaborador nao encontrado.", "danger")
        return redirect(url_for("index"))

    month_start = date(year, month, 1)
    month_end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)

    entries = (
        HourEntry.query
        .filter(
            HourEntry.collaborator_id == collaborator_id,
            HourEntry.entry_date >= month_start,
            HourEntry.entry_date < month_end,
        )
        .order_by(HourEntry.entry_date.asc())
        .all()
    )

    positive = sum((Decimal(e.hours) for e in entries if e.hours >= 0), Decimal("0"))
    negative = sum((abs(Decimal(e.hours)) for e in entries if e.hours < 0), Decimal("0"))
    net = positive - negative
    days = int(max(net, Decimal("0")) * 60 // 440)

    global_daily_rate = Decimal(get_setting("daily_rate", "0"))
    daily_rate = Decimal(collab.daily_rate) if collab.daily_rate is not None else global_daily_rate
    total_value = Decimal(days) * daily_rate if daily_rate > 0 else Decimal("0")

    month_name = _MESES_PT[month]

    html = render_template(
        "pdf_collab.html",
        collab=collab,
        entries=entries,
        month_name=month_name,
        year=year,
        month=month,
        positive=positive,
        negative=negative,
        net=net,
        days=days,
        daily_rate=daily_rate,
        total_value=total_value,
    )
    try:
        from weasyprint import HTML  # type: ignore[import-untyped]
        pdf_bytes = HTML(string=html, base_url=request.host_url).write_pdf()
        response = make_response(pdf_bytes)
        response.headers["Content-Type"] = "application/pdf"
        response.headers["Content-Disposition"] = (
            f'inline; filename="{collab.name.replace(" ","_")}_{year}_{month:02d}.pdf"'
        )
        return response
    except Exception as exc:  # noqa: BLE001
        flash(f"Erro ao gerar PDF: {exc}", "danger")
        return redirect(url_for("collaborator_history", collaborator_id=collaborator_id))


@app.get("/summary/pdf")
def summary_pdf():
    """Exporta o resumo mensal em PDF usando WeasyPrint."""
    year, month = _parse_month_param(request.args.get("month"))
    cards, totals = monthly_summary(year, month)
    month_name = _MESES_PT[month]

    # lançamentos detalhados agrupados por colaborador
    month_start = date(year, month, 1)
    if month == 12:
        month_end = date(year + 1, 1, 1)
    else:
        month_end = date(year, month + 1, 1)
    raw_entries = (
        db.session.query(HourEntry, Collaborator)
        .join(Collaborator, Collaborator.id == HourEntry.collaborator_id)
        .filter(
            HourEntry.entry_date >= month_start,
            HourEntry.entry_date < month_end,
        )
        .order_by(Collaborator.name.asc(), HourEntry.entry_date.asc())
        .all()
    )
    from collections import defaultdict as _dd
    detail: dict[str, list[dict]] = _dd(list)
    for entry, collab in raw_entries:
        detail[collab.name].append({
            "date": entry.entry_date.strftime("%d/%m/%Y"),
            "hours": float(entry.hours),
            "note": entry.note or "",
        })

    html = render_template(
        "pdf_summary.html",
        cards=cards,
        totals=totals,
        month_name=month_name,
        year=year,
        detail=detail,
    )
    try:
        from weasyprint import HTML  # type: ignore[import-untyped]

        pdf_bytes = HTML(string=html, base_url=request.host_url).write_pdf()
        response = make_response(pdf_bytes)
        response.headers["Content-Type"] = "application/pdf"
        response.headers["Content-Disposition"] = (
            f'inline; filename="multimax_{year}_{month:02d}.pdf"'
        )
        return response
    except Exception as exc:  # noqa: BLE001
        flash(f"Erro ao gerar PDF: {exc}", "danger")
        return redirect(url_for("index", month=f"{year}-{month:02d}"))


@app.route("/whatsapp/resumo", methods=["GET", "POST"])
@login_required
def whatsapp_resumo():
    if request.method == "GET":
        return redirect(url_for("index"))
    """Dispara o resumo geral dos colaboradores do mes para o WhatsApp."""
    year, month = _parse_month_param(request.form.get("month"))
    cards, totals = monthly_summary(year, month)
    if not cards:
        flash("Sem dados no mes selecionado para enviar.", "warning")
        return redirect(url_for("index", month=f"{year}-{month:02d}"))
    wz.resumo_geral(cards, totals)
    flash("Resumo enviado para o grupo WhatsApp.", "success")
    return redirect(url_for("index", month=f"{year}-{month:02d}"))


@app.route("/whatsapp/pdf", methods=["GET", "POST"])
@login_required
def whatsapp_pdf():
    if request.method == "GET":
        return redirect(url_for("index"))
    """Gera o PDF do resumo mensal e envia para o grupo WhatsApp."""
    year, month = _parse_month_param(request.form.get("month"))
    cards, totals = monthly_summary(year, month)
    if not cards:
        flash("Sem dados no mes selecionado para gerar PDF.", "warning")
        return redirect(url_for("index", month=f"{year}-{month:02d}"))

    month_name = _MESES_PT[month]
    month_start = date(year, month, 1)
    month_end = (
        date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    )
    raw_entries = (
        db.session.query(HourEntry, Collaborator)
        .join(Collaborator, Collaborator.id == HourEntry.collaborator_id)
        .filter(
            HourEntry.entry_date >= month_start,
            HourEntry.entry_date < month_end,
        )
        .order_by(Collaborator.name.asc(), HourEntry.entry_date.asc())
        .all()
    )
    from collections import defaultdict as _dd2
    detail: dict[str, list[dict]] = _dd2(list)
    for entry, collab in raw_entries:
        detail[collab.name].append({
            "date": entry.entry_date.strftime("%d/%m/%Y"),
            "hours": float(entry.hours),
            "note": entry.note or "",
        })

    html = render_template(
        "pdf_summary.html",
        cards=cards,
        totals=totals,
        month_name=month_name,
        year=year,
        detail=detail,
    )
    try:
        from weasyprint import HTML  # type: ignore[import-untyped]

        pdf_bytes: bytes = HTML(string=html, base_url=request.host_url).write_pdf() or b""
        filename = f"multimax_{year}_{month:02d}.pdf"
        wz.send_pdf(pdf_bytes, filename, totals["month_label"])
        flash("PDF enviado para o grupo WhatsApp.", "success")
    except Exception as exc:  # noqa: BLE001
        flash(f"Erro ao gerar PDF: {exc}", "danger")
    return redirect(url_for("index", month=f"{year}-{month:02d}"))


@app.route("/offline")
def offline() -> str:
    """Pagina exibida pelo service worker quando sem conexao."""
    return render_template("offline.html")


# ---------------------------------------------------------------------------
# Ponto eletronico — autenticacao de colaborador
# ---------------------------------------------------------------------------


def ponto_required(f):
    """Permite acesso se admin autenticado OU colaborador com sessao de ponto."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if current_user.is_authenticated:
            return f(*args, **kwargs)
        if _flask_session.get("ponto_collab_id"):
            return f(*args, **kwargs)
        return redirect(url_for("ponto_login", next=request.path))
    return wrapper


@app.get("/ponto/login")
def ponto_login():
    """Pagina de login para colaboradores acessarem a captura de ponto."""
    if current_user.is_authenticated or _flask_session.get("ponto_collab_id"):
        return redirect(url_for("ponto_camera"))
    return render_template("ponto_login.html")


@app.post("/ponto/login")
def ponto_login_post():
    """Autentica colaborador pelo nome e senha de ponto."""
    nome = (request.form.get("nome") or "").strip()
    senha = request.form.get("senha") or ""

    if not nome or not senha:
        flash("Informe nome e senha.", "warning")
        return redirect(url_for("ponto_login"))

    # Busca por nome exato (case-insensitive) entre colaboradores ativos
    collab = Collaborator.query.filter(
        func.lower(Collaborator.name) == nome.lower(),
        Collaborator.active == True,  # noqa: E712
        Collaborator.ponto_password_hash != None,  # noqa: E711
    ).first()

    if not collab or not check_password_hash(collab.ponto_password_hash, senha):
        flash("Nome ou senha invalidos.", "danger")
        return redirect(url_for("ponto_login"))

    _flask_session["ponto_collab_id"] = collab.id
    _flask_session["ponto_collab_name"] = collab.name
    flash(f"Ola, {collab.name}! Pronto para registrar seu ponto.", "success")
    next_url = request.form.get("next") or url_for("ponto_camera")
    return redirect(next_url)


@app.route("/ponto/logout-ponto", methods=["GET", "POST"])
def ponto_logout():
    if request.method == "GET":
        return redirect(url_for("ponto_login"))
    """Encerra a sessao do colaborador de ponto."""
    nome = _flask_session.pop("ponto_collab_name", "")
    _flask_session.pop("ponto_collab_id", None)
    flash(f"Ate logo, {nome}!" if nome else "Sessao encerrada.", "info")
    return redirect(url_for("ponto_login"))


@app.route("/ponto/recuperar-senha", methods=["GET", "POST"])
def ponto_recuperar_senha():
    if request.method == "GET":
        return redirect(url_for("ponto_login"))
    """Envia senha temporária para o WhatsApp do colaborador."""
    nome = (request.form.get("nome") or "").strip()
    if not nome:
        flash("Informe seu nome para recuperar a senha.", "warning")
        return redirect(url_for("ponto_login"))

    collab = Collaborator.query.filter(
        func.lower(Collaborator.name) == nome.lower(),
        Collaborator.active == True,  # noqa: E712
        Collaborator.ponto_password_hash != None,  # noqa: E711
    ).first()

    # Mensagem genérica para não revelar se o usuário existe
    msg_ok = "Se o nome estiver cadastrado com WhatsApp, uma senha temporária foi enviada."

    if not collab or not collab.whatsapp:
        flash(msg_ok, "info")
        return redirect(url_for("ponto_login"))

    # Gera senha temporária: 6 caracteres alfanuméricos maiúsculos
    alphabet = string.ascii_uppercase + string.digits
    temp_senha = "".join(secrets.choice(alphabet) for _ in range(6))

    collab.ponto_password_hash = generate_password_hash(temp_senha)
    db.session.commit()

    wz.send(
        f"🔑 *Senha temporária de ponto*\n"
        f"Olá, {collab.name}!\n\n"
        f"Sua nova senha temporária é: *{temp_senha}*\n\n"
        f"Acesse o ponto e altere-a imediatamente após entrar.",
        origin="ponto_recuperar_senha",
        para=collab.whatsapp,
    )

    flash(msg_ok, "info")
    return redirect(url_for("ponto_login"))


@app.get("/api/suggest-password")
@login_required
def api_suggest_password():
    """Retorna senha sugerida para um colaborador (admin only)."""
    name = (request.args.get("name") or "").strip()
    if not name:
        return jsonify({"error": "nome obrigatorio"}), 400
    return jsonify({"senha": suggest_ponto_password(name)})


@app.route("/collaborators/<int:collaborator_id>/set-ponto-password", methods=["GET", "POST"])
@login_required
def set_ponto_password(collaborator_id: int):
    if request.method == "GET":
        return redirect(url_for("collaborator_history", collaborator_id=collaborator_id))
    """Define ou redefine a senha de ponto de um colaborador (admin only)."""
    collab = db.session.get(Collaborator, collaborator_id)
    if not collab:
        flash("Colaborador nao encontrado.", "danger")
        return redirect(url_for("index"))
    senha = (request.form.get("ponto_password") or "").strip()
    if not senha:
        flash("Informe uma senha.", "warning")
        return redirect(url_for("collaborator_history", collaborator_id=collaborator_id))
    if len(senha) < 3:
        flash("Senha muito curta (minimo 3 caracteres).", "warning")
        return redirect(url_for("collaborator_history", collaborator_id=collaborator_id))
    collab.ponto_password_hash = generate_password_hash(senha)
    db.session.commit()
    flash(f"Senha de ponto de {collab.name} atualizada.", "success")
    return redirect(url_for("collaborator_history", collaborator_id=collaborator_id))


@app.route("/collaborators/<int:collaborator_id>/make-admin", methods=["GET", "POST"])
@login_required
def make_collaborator_admin(collaborator_id: int):
    """Cria uma conta de administrador para o colaborador."""
    if request.method == "GET":
        return redirect(url_for("index"))
    collab = db.session.get(Collaborator, collaborator_id)
    if not collab:
        flash("Colaborador nao encontrado.", "danger")
        return redirect(url_for("index"))

    username = (request.form.get("admin_username") or "").strip()
    password = (request.form.get("admin_password") or "").strip()

    if not username or not password:
        flash("Usuario e senha sao obrigatorios.", "warning")
        return redirect(url_for("index"))

    if len(password) < 4:
        flash("Senha deve ter no minimo 4 caracteres.", "warning")
        return redirect(url_for("index"))

    if User.query.filter(func.lower(User.username) == username.lower()).first():
        flash(f"Usuario '{username}' ja existe.", "warning")
        return redirect(url_for("index"))

    db.session.add(User(
        username=username,
        password_hash=generate_password_hash(password),
    ))
    db.session.commit()
    flash(f"Admin '{username}' criado para {collab.name}.", "success")
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# Ponto eletronico
# ---------------------------------------------------------------------------

def _allowed_image(filename: str) -> bool:
    """Verifica se a extensao do arquivo e uma imagem permitida."""
    return os.path.splitext(filename)[-1].lower() in _ALLOWED_IMAGE_EXTS


@app.get("/ponto")
def ponto():
    """Exibe a pagina de registro de ponto eletronico via foto."""
    _PER_PAGE = 10
    page = request.args.get("page", 1, type=int)

    total_count = PunchRecord.query.count()
    linked_count = PunchRecord.query.filter(PunchRecord.collaborator_id.isnot(None)).count()
    pending_count = total_count - linked_count

    pending_records = (
        PunchRecord.query
        .filter(PunchRecord.collaborator_id.is_(None))
        .order_by(PunchRecord.punch_date.desc(), PunchRecord.punch_time.desc(), PunchRecord.id.desc())
        .all()
    )

    pagination = (
        PunchRecord.query
        .order_by(
            PunchRecord.punch_date.desc(),
            PunchRecord.punch_time.desc(),
            PunchRecord.id.desc(),
        )
        .paginate(page=page, per_page=_PER_PAGE, error_out=False)
    )

    collaborators = (
        Collaborator.query
        .filter_by(active=True)
        .order_by(Collaborator.name.asc())
        .all()
    )
    return render_template(
        "ponto.html",
        total_count=total_count,
        linked_count=linked_count,
        pending_count=pending_count,
        pending_records=pending_records,
        pagination=pagination,
        collaborators=collaborators,
    )


@app.get("/ponto/camera")
@ponto_required
def ponto_camera():
    """Pagina de captura ao vivo — camera com analise OCR em tempo real."""
    return render_template("ponto_camera.html")


@app.get("/ponto/buscar-cpf")
def ponto_buscar_cpf():
    """Retorna colaborador pelo CPF. Se nao encontrado, retorna lista de collabs sem CPF."""
    cpf = re.sub(r"\D", "", request.args.get("cpf", ""))
    if len(cpf) != 11:
        return jsonify({}), 200
    collab = Collaborator.query.filter_by(cpf=cpf, active=True).first()
    if collab:
        return jsonify({"found": True, "id": collab.id, "nome": collab.name})
    # Nao encontrado — retorna colaboradores ativos sem CPF cadastrado
    sem_cpf = (
        Collaborator.query
        .filter_by(active=True)
        .filter(db.or_(Collaborator.cpf == None, Collaborator.cpf == ""))  # noqa: E711
        .order_by(Collaborator.name.asc())
        .all()
    )
    return jsonify({
        "found": False,
        "sem_cpf": [{"id": c.id, "nome": c.name} for c in sem_cpf],
    }), 200


@app.post("/ponto/associar-cpf")
@ponto_required
def ponto_associar_cpf():
    """Associa um CPF a um colaborador existente."""
    cpf = re.sub(r"\D", "", request.json.get("cpf", "") if request.is_json else request.form.get("cpf", ""))
    collab_id = request.json.get("collaborator_id") if request.is_json else request.form.get("collaborator_id")
    if not cpf or len(cpf) != 11 or not collab_id:
        return jsonify({"error": "dados invalidos"}), 400
    collab = db.session.get(Collaborator, int(collab_id))
    if not collab:
        return jsonify({"error": "colaborador nao encontrado"}), 404
    collab.cpf = cpf
    db.session.commit()
    return jsonify({"id": collab.id, "nome": collab.name}), 200


@app.post("/ponto/criar-colaborador")
@login_required
def ponto_criar_colaborador():
    """Cria novo colaborador com nome e CPF vindos do registro de ponto."""
    data = request.json if request.is_json else request.form
    nome = (data.get("nome") or "").strip()
    cpf = re.sub(r"\D", "", data.get("cpf") or "")
    if not nome or len(cpf) != 11:
        return jsonify({"error": "nome e CPF sao obrigatorios"}), 400
    if Collaborator.query.filter_by(cpf=cpf).first():
        return jsonify({"error": "CPF ja cadastrado"}), 409
    collab = Collaborator(name=nome, cpf=cpf)
    db.session.add(collab)
    db.session.commit()
    return jsonify({"id": collab.id, "nome": collab.name}), 201


@app.post("/ponto/ocr-live")
@ponto_required
def ponto_ocr_live():
    """Analisa frame da camera sem salvar — retorna campos detectados como JSON."""
    import tempfile as _tmp
    frame = request.files.get("frame")
    if not frame:
        return jsonify({"error": "no frame"}), 400
    with _tmp.NamedTemporaryFile(suffix=".jpg", delete=False) as tf:
        frame.save(tf.name)
        tmp_path = tf.name
    try:
        data = ponto_ocr.ocr_image(tmp_path, use_vision=False)  # Tesseract: rapido para frames
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    return jsonify({
        "cpf":   data.cpf,
        "data":  data.data,
        "hora":  data.hora,
        "nsr":   data.nsr,
        "nome":  data.nome,
        "score": sum(bool(x) for x in [data.cpf, data.data, data.hora, data.nsr]),
    })


@app.route("/ponto/upload", methods=["GET", "POST"])
@ponto_required
def ponto_upload():
    """Recebe foto do comprovante, executa OCR e exibe pagina de confirmacao."""
    if request.method == "GET":
        return redirect(url_for("ponto_camera"))
    file = request.files.get("foto")
    if not file or not file.filename:
        flash("Nenhuma imagem enviada.", "warning")
        return redirect(url_for("ponto"))

    if not _allowed_image(file.filename):
        flash("Formato nao suportado. Envie JPG, PNG, WEBP ou JFIF.", "warning")
        return redirect(url_for("ponto"))

    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    ext = os.path.splitext(file.filename)[-1].lower() or ".jpg"
    filename = f"{uuid.uuid4().hex}{ext}"
    filepath = os.path.join(UPLOAD_FOLDER, filename)
    file.save(filepath)

    data = ponto_ocr.ocr_image(filepath)

    if data.error:
        flash(f"⚠ OCR parcial: {data.error} Preencha ou corrija os campos abaixo.", "warning")

    # Pre-seleciona colaborador pelo CPF se existir
    cpf_clean = re.sub(r"\D", "", data.cpf or "")
    collab = Collaborator.query.filter_by(cpf=cpf_clean).first() if cpf_clean else None

    # Conta batidas nao processadas do colaborador naquele dia (se CPF encontrado)
    existing_count = 0
    if collab and data.data:
        try:
            d = datetime.strptime(data.data, "%d/%m/%Y").date()
            existing_count = PunchRecord.query.filter_by(
                collaborator_id=collab.id,
                punch_date=d,
                processed=False,
            ).count()
        except ValueError:
            pass

    collaborators = (
        Collaborator.query
        .filter_by(active=True)
        .order_by(Collaborator.name.asc())
        .all()
    )

    # Turnos do colaborador para sugestão automática de tipo de batida
    sched_turnos = _get_schedule(collab) if collab else []

    # Batidas já registradas no dia (para saber qual tipo vem a seguir)
    existing_punches: list[PunchRecord] = []
    if collab and data.data:
        try:
            _pd = datetime.strptime(data.data, "%d/%m/%Y").date()
            existing_punches = (
                PunchRecord.query
                .filter_by(collaborator_id=collab.id, punch_date=_pd)
                .order_by(PunchRecord.punch_time.asc())
                .all()
            )
        except ValueError:
            pass

    return render_template(
        "ponto_confirmar.html",
        data=data,
        cpf_clean=cpf_clean,
        filename=filename,
        collab=collab,
        collaborators=collaborators,
        existing_count=existing_count,
        sched_turnos=sched_turnos,
        existing_punches=[
            {"type": p.punch_type, "time": p.punch_time.strftime("%H:%M")}
            for p in existing_punches
        ],
    )


def _try_register_interval(
    collab_id: int,
    punch_date: date,
    gives_folga: bool = False,
) -> str | None:
    """Reprocessa TODOS os registros de ponto do colaborador no dia.

    Apaga HourEntries anteriores gerados por ponto e recria os pares
    em ordem cronologica (1a+2a batida, 3a+4a, etc.).
    Retorna descricao dos intervalos gerados ou None se nao houver par.
    """
    # Busca todos os registros do dia (processados ou nao)
    all_punches = (
        PunchRecord.query
        .filter_by(collaborator_id=collab_id, punch_date=punch_date)
        .order_by(PunchRecord.punch_time.asc())
        .all()
    )

    if len(all_punches) < 2:
        # Marca como nao processado para aguardar a proxima batida
        for p in all_punches:
            p.processed = False
        return None

    # Remove HourEntries gerados por registros de ponto neste dia
    existing_entries = (
        HourEntry.query
        .filter_by(collaborator_id=collab_id, entry_date=punch_date)
        .filter(HourEntry.note.like("Ponto:%"))
        .all()
    )
    for e in existing_entries:
        db.session.delete(e)

    # Desmarca todos como nao processados
    for p in all_punches:
        p.processed = False

    # Emparelha em ordem cronologica: (0,1), (2,3), (4,5)...
    intervals = []
    i = 0
    while i + 1 < len(all_punches):
        p1, p2 = all_punches[i], all_punches[i + 1]
        t1 = datetime.combine(punch_date, p1.punch_time)
        t2 = datetime.combine(punch_date, p2.punch_time)
        t_entry = min(t1, t2)
        t_exit  = max(t1, t2)

        seconds = (t_exit - t_entry).total_seconds()
        hours   = Decimal(str(round(seconds / 3600, 2)))

        note = (
            f"Ponto: {t_entry.strftime('%H:%M')} \u2192 "
            f"{t_exit.strftime('%H:%M')} (comprovante)"
        )
        db.session.add(
            HourEntry(
                collaborator_id=collab_id,
                entry_date=punch_date,
                hours=hours,
                note=note,
                gives_folga=gives_folga,
            )
        )
        p1.processed = True
        p2.processed = True
        intervals.append(
            f"{t_entry.strftime('%H:%M')}\u2192{t_exit.strftime('%H:%M')} ({hours}h)"
        )
        i += 2

    if not intervals:
        return None

    if gives_folga:
        collab = db.session.get(Collaborator, collab_id)
        if collab:
            collab.folga_days += 1

    return ", ".join(intervals)


@app.post("/ponto/confirmar")
@ponto_required
def ponto_confirmar():
    """Persiste o registro de ponto confirmado e calcula intervalo se aplicavel."""
    filename = (request.form.get("filename") or "").strip()
    raw_name = (request.form.get("raw_name") or "").strip()
    raw_cpf = re.sub(r"\D", "", request.form.get("raw_cpf") or "")
    data_str = (request.form.get("data") or "").strip()
    hora_str = (request.form.get("hora") or "").strip()
    nsr = (request.form.get("nsr") or "").strip()
    nrep = (request.form.get("nrep") or "").strip() or None
    ad_key = (request.form.get("ad_key") or "").strip() or None
    gives_folga = bool(request.form.get("gives_folga"))
    collab_id_raw = (request.form.get("collaborator_id") or "").strip()
    punch_type = (request.form.get("punch_type") or "").strip() or None
    origin = "admin" if current_user.is_authenticated else "automatico"
    gives_folga = bool(request.form.get("gives_folga"))

    try:
        punch_date = datetime.strptime(data_str, "%d/%m/%Y").date()
    except ValueError:
        flash(f"Data invalida: {data_str}", "danger")
        return redirect(url_for("ponto"))

    try:
        punch_time = datetime.strptime(hora_str, "%H:%M").time()
    except ValueError:
        flash(f"Hora invalida: {hora_str}", "danger")
        return redirect(url_for("ponto"))

    if not raw_cpf or len(raw_cpf) != 11:
        flash("CPF obrigatorio — informe os 11 digitos do CPF antes de confirmar.", "danger")
        return redirect(url_for("ponto"))

    if not nsr:
        flash("NSR ausente — o registro nao pode ser salvo sem a chave NSR.", "danger")
        return redirect(url_for("ponto"))

    existing = PunchRecord.query.filter_by(nsr=nsr).first()
    if existing:
        flash(
            f"Comprovante ja registrado (NSR {nsr}, "
            f"{existing.punch_date.strftime('%d/%m/%Y')} "
            f"{existing.punch_time.strftime('%H:%M')}).",
            "warning",
        )
        return redirect(url_for("ponto"))

    # Colaborador obrigatorio
    collab_id: int | None = None
    if collab_id_raw:
        try:
            collab_id = int(collab_id_raw)
        except ValueError:
            pass
    if not collab_id and raw_cpf:
        c = Collaborator.query.filter_by(cpf=raw_cpf).first()
        if c:
            collab_id = c.id
    if not collab_id:
        flash("Nenhum colaborador vinculado ao CPF informado. Associe ou cadastre o colaborador antes de registrar.", "danger")
        return redirect(url_for("ponto"))

    record = PunchRecord(
        collaborator_id=collab_id,
        raw_cpf=raw_cpf,
        raw_name=raw_name,
        punch_date=punch_date,
        punch_time=punch_time,
        nsr=nsr,
        nrep=nrep,
        ad_key=ad_key,
        image_filename=filename or None,
        punch_type=punch_type or None,
        origin=origin,
        gives_folga=gives_folga,
    )
    db.session.add(record)
    db.session.flush()  # persiste id sem commit final

    interval_msg: str | None = None
    if collab_id:
        interval_msg = _try_register_interval(collab_id, punch_date, gives_folga)

    db.session.commit()

    collab = db.session.get(Collaborator, collab_id) if collab_id else None
    collab_label = collab.name if collab else f"CPF {raw_cpf}"

    if interval_msg:
        flash(
            f"Ponto registrado: {collab_label} — {data_str} {hora_str}. "
            f"Intervalo lancado: {interval_msg}.",
            "success",
        )
    elif collab_id:
        flash(
            f"Ponto registrado: {collab_label} — {data_str} {hora_str}. "
            "Aguardando segunda batida para calcular intervalo.",
            "success",
        )
    else:
        flash(
            f"Ponto salvo ({data_str} {hora_str}). "
            f"CPF {raw_cpf} nao vinculado — vincule manualmente.",
            "warning",
        )

    # Notificação WhatsApp — apenas para o número pessoal do colaborador
    if collab and collab.whatsapp:
        wz.ponto_registrado(
            collab.name,
            data_str,
            hora_str,
            punch_type or "—",
            origin,
            para=collab.whatsapp,
        )
        # Verifica jornada incompleta no dia
        all_day = (
            PunchRecord.query
            .filter_by(collaborator_id=collab_id, punch_date=punch_date)
            .order_by(PunchRecord.punch_time.asc())
            .all()
        )
        day_result = _process_punches_dia(all_day)
        if day_result["incompleto"]:
            wz.jornada_incompleta(collab.name, data_str, para=collab.whatsapp)

    # Lembrete agendado por horário definido
    if collab:
        _handle_schedule_alerts(collab, punch_date, punch_type, data_str)

    return redirect(url_for("ponto"))


@app.route("/ponto/<int:record_id>/vincular", methods=["GET", "POST"])
@login_required
def ponto_vincular(record_id: int):
    if request.method == "GET":
        return redirect(url_for("ponto"))
    """Vincula um registro de ponto a um colaborador existente."""
    record = db.session.get(PunchRecord, record_id)
    if not record:
        flash("Registro nao encontrado.", "danger")
        return redirect(url_for("ponto"))

    try:
        collab_id = int(request.form.get("collaborator_id") or "")
    except ValueError:
        flash("Selecione um colaborador valido.", "warning")
        return redirect(url_for("ponto"))

    collab = db.session.get(Collaborator, collab_id)
    if not collab:
        flash("Colaborador nao encontrado.", "danger")
        return redirect(url_for("ponto"))

    record.collaborator_id = collab.id
    db.session.commit()
    flash(f"Ponto vinculado a {collab.name}.", "success")
    return redirect(url_for("ponto"))


@app.route("/ponto/<int:record_id>/delete", methods=["GET", "POST"])
@login_required
def ponto_delete(record_id: int):
    if request.method == "GET":
        return redirect(url_for("ponto"))
    """Remove um registro de ponto."""
    record = db.session.get(PunchRecord, record_id)
    if not record:
        flash("Registro nao encontrado.", "danger")
        return redirect(url_for("ponto"))
    db.session.delete(record)
    db.session.commit()
    flash("Registro de ponto removido.", "info")
    return redirect(url_for("ponto"))


@app.get("/ponto/uploads/<path:filename>")
def ponto_image(filename: str):
    """Serve as imagens dos comprovantes enviados."""
    from flask import send_from_directory
    return send_from_directory(UPLOAD_FOLDER, filename)


# ---------------------------------------------------------------------------
# Correção de Jornadas Incompletas (admin)
# ---------------------------------------------------------------------------

@app.get("/api/colaborador/<int:collab_id>/ponto-dia")
@login_required
def api_ponto_dia(collab_id: int):
    """Retorna as batidas de um dia específico e o diagnóstico de incompletude."""
    collab = db.session.get(Collaborator, collab_id)
    if not collab:
        return jsonify({"error": "colaborador não encontrado"}), 404

    date_str = request.args.get("date", "")
    try:
        target_date = date.fromisoformat(date_str)
    except ValueError:
        return jsonify({"error": "data inválida"}), 400

    punches = (
        PunchRecord.query
        .filter_by(collaborator_id=collab_id, punch_date=target_date)
        .order_by(PunchRecord.punch_time.asc())
        .all()
    )

    result = _process_punches_dia(punches)

    TIPOS_LABEL = {
        "entrada": "Entrada",
        "intervalo_saida": "Saída p/ Intervalo",
        "intervalo_retorno": "Retorno do Intervalo",
        "saida_final": "Saída Final",
        "extra": "Extra",
        None: "Legado",
    }

    # Diagnóstico textual
    diagnostico = []
    if result["incompleto"]:
        all_legacy = all(p.punch_type is None for p in punches)
        if all_legacy:
            diagnostico.append(f"Número ímpar de batidas ({len(punches)}): última batida sem par de saída.")
        else:
            by_type: dict = {}
            for p in punches:
                by_type.setdefault(p.punch_type or "extra", []).append(p)
            entradas = by_type.get("entrada", [])
            int_saidas = by_type.get("intervalo_saida", [])
            int_retornos = by_type.get("intervalo_retorno", [])
            saidas = by_type.get("saida_final", [])
            extras = by_type.get("extra", [])
            if entradas and not int_saidas and not saidas:
                diagnostico.append("Entrada registrada sem saída final.")
            elif entradas and int_saidas and int_retornos and not saidas:
                diagnostico.append("Retorno do intervalo registrado sem saída final.")
            elif entradas and int_saidas and not int_retornos and not saidas:
                diagnostico.append("Saída para intervalo registrada sem retorno nem saída final.")
            if len(extras) % 2 != 0:
                diagnostico.append(f"Batidas extras em número ímpar ({len(extras)}).")
        if not diagnostico:
            diagnostico.append("Jornada incompleta: verifique as batidas abaixo.")

    return jsonify({
        "collab_name": collab.name,
        "date": target_date.isoformat(),
        "date_br": target_date.strftime("%d/%m/%Y"),
        "incompleto": result["incompleto"],
        "diagnostico": diagnostico,
        "punches": [
            {
                "id": p.id,
                "time": p.punch_time.strftime("%H:%M"),
                "punch_type": p.punch_type,
                "type_label": TIPOS_LABEL.get(p.punch_type, p.punch_type or "Legado"),
                "origin": p.origin or "automatico",
                "nsr": p.nsr,
            }
            for p in punches
        ],
    })


@app.route("/colaborador/<int:collab_id>/ponto-dia/add", methods=["GET", "POST"])
@login_required
def admin_ponto_dia_add(collab_id: int):
    if request.method == "GET":
        return redirect(url_for("collaborator_history", collaborator_id=collab_id))
    """Adiciona uma batida manual para correção de jornada incompleta (admin)."""
    collab = db.session.get(Collaborator, collab_id)
    if not collab:
        flash("Colaborador não encontrado.", "danger")
        return redirect(url_for("index"))

    date_str = (request.form.get("date") or "").strip()
    time_str = (request.form.get("time") or "").strip()
    punch_type = (request.form.get("punch_type") or "").strip() or None
    month_param = (request.form.get("month") or "").strip()

    try:
        target_date = date.fromisoformat(date_str)
    except ValueError:
        flash("Data inválida.", "danger")
        return redirect(url_for("collaborator_history", collaborator_id=collab_id, month=month_param))

    try:
        target_time = datetime.strptime(time_str, "%H:%M").time()
    except ValueError:
        flash("Hora inválida.", "danger")
        return redirect(url_for("collaborator_history", collaborator_id=collab_id, month=month_param))

    # NSR único gerado para registros manuais
    nsr_manual = f"MANUAL-{uuid.uuid4().hex[:16].upper()}"

    record = PunchRecord(
        collaborator_id=collab_id,
        raw_cpf=collab.cpf or "00000000000",
        raw_name=collab.name,
        punch_date=target_date,
        punch_time=target_time,
        nsr=nsr_manual,
        punch_type=punch_type,
        origin="admin",
        gives_folga=False,
    )
    db.session.add(record)
    db.session.commit()

    flash(
        f"Batida manual adicionada: {collab.name} — {target_date.strftime('%d/%m/%Y')} {time_str}.",
        "success",
    )
    return redirect(url_for("collaborator_history", collaborator_id=collab_id, month=month_param))


@app.route("/colaborador/<int:collab_id>/ponto/<int:record_id>/excluir", methods=["GET", "POST"])
@login_required
def admin_ponto_excluir(collab_id: int, record_id: int):
    if request.method == "GET":
        return redirect(url_for("collaborator_history", collaborator_id=collab_id))
    record = db.session.get(PunchRecord, record_id)
    if not record or record.collaborator_id != collab_id:
        flash("Registro não encontrado.", "danger")
        return redirect(url_for("collaborator_history", collaborator_id=collab_id))

    month_param = (request.form.get("month") or "").strip()
    db.session.delete(record)
    db.session.commit()
    flash("Batida removida.", "info")
    return redirect(url_for("collaborator_history", collaborator_id=collab_id, month=month_param))


@app.route("/sw.js")
def service_worker():
    """Serve o Service Worker com escopo raiz permitido."""
    response = make_response(
        app.send_static_file("sw.js")
    )
    response.headers["Service-Worker-Allowed"] = "/"
    response.headers["Content-Type"] = "application/javascript"
    return response


# ---------------------------------------------------------------------------
# Tela de escolha de acesso (Bloco 7 — UX de Login)
# ---------------------------------------------------------------------------

@app.get("/acesso")
def acesso():
    """Tela intermediária: escolha entre Administrador e Colaborador."""
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    if _flask_session.get("ponto_collab_id"):
        return redirect(url_for("ponto_camera"))
    return render_template("login_choice.html")


# ---------------------------------------------------------------------------
# Gestão de Feriados (admin only — Bloco 6)
# ---------------------------------------------------------------------------

@app.get("/feriados")
@login_required
def feriados_list():
    """Lista feriados cadastrados."""
    feriados = Holiday.query.order_by(Holiday.holiday_date.asc()).all()
    return render_template("feriados.html", feriados=feriados)


@app.route("/feriados/create", methods=["GET", "POST"])
@login_required
def feriado_create():
    if request.method == "GET":
        return redirect(url_for("feriados_list"))
    """Cadastra um novo feriado e recalcula metas automaticamente."""
    date_raw = (request.form.get("holiday_date") or "").strip()
    descricao = (request.form.get("descricao") or "").strip()
    if not date_raw or not descricao:
        flash("Data e descrição são obrigatórios.", "warning")
        return redirect(url_for("feriados_list"))
    try:
        holiday_date = datetime.strptime(date_raw, "%Y-%m-%d").date()
    except ValueError:
        flash("Data inválida.", "danger")
        return redirect(url_for("feriados_list"))
    if Holiday.query.filter_by(holiday_date=holiday_date).first():
        flash(
            f"Feriado em {holiday_date.strftime('%d/%m/%Y')} já cadastrado.", "warning"
        )
        return redirect(url_for("feriados_list"))
    db.session.add(Holiday(holiday_date=holiday_date, descricao=descricao))
    db.session.commit()
    flash(
        f"Feriado '{descricao}' em {holiday_date.strftime('%d/%m/%Y')} cadastrado. "
        "Metas recalculadas automaticamente.",
        "success",
    )
    return redirect(url_for("feriados_list"))


@app.route("/feriados/<int:feriado_id>/delete", methods=["GET", "POST"])
@login_required
def feriado_delete(feriado_id: int):
    if request.method == "GET":
        return redirect(url_for("feriados_list"))
    """Remove um feriado cadastrado."""
    f = db.session.get(Holiday, feriado_id)
    if not f:
        flash("Feriado não encontrado.", "warning")
        return redirect(url_for("feriados_list"))
    desc = f.descricao
    db.session.delete(f)
    db.session.commit()
    flash(f"Feriado '{desc}' removido.", "info")
    return redirect(url_for("feriados_list"))


@app.route("/feriados/<int:feriado_id>/toggle", methods=["GET", "POST"])
@login_required
def feriado_toggle(feriado_id: int):
    """Ativa ou desativa um feriado (sem remover)."""
    f = db.session.get(Holiday, feriado_id)
    if not f:
        flash("Feriado não encontrado.", "warning")
        return redirect(url_for("feriados_list"))
    f.ativo = not f.ativo
    db.session.commit()
    status = "ativado" if f.ativo else "desativado"
    flash(f"Feriado '{f.descricao}' {status}.", "info")
    return redirect(url_for("feriados_list"))


@app.route("/feriados/<int:feriado_id>/edit", methods=["GET", "POST"])
@login_required
def feriado_edit(feriado_id: int):
    """Edita descrição e/ou data de um feriado."""
    f = db.session.get(Holiday, feriado_id)
    if not f:
        flash("Feriado não encontrado.", "warning")
        return redirect(url_for("feriados_list"))
    date_raw = (request.form.get("holiday_date") or "").strip()
    descricao = (request.form.get("descricao") or "").strip()
    if not date_raw or not descricao:
        flash("Data e descrição são obrigatórios.", "warning")
        return redirect(url_for("feriados_list"))
    try:
        new_date = datetime.strptime(date_raw, "%Y-%m-%d").date()
    except ValueError:
        flash("Data inválida.", "danger")
        return redirect(url_for("feriados_list"))
    conflict = Holiday.query.filter(
        Holiday.holiday_date == new_date, Holiday.id != feriado_id
    ).first()
    if conflict:
        flash(f"Já existe um feriado em {new_date.strftime('%d/%m/%Y')}.", "warning")
        return redirect(url_for("feriados_list"))
    f.holiday_date = new_date
    f.descricao = descricao
    db.session.commit()
    flash("Feriado atualizado.", "success")
    return redirect(url_for("feriados_list"))


# ---------------------------------------------------------------------------
# Painel do Colaborador — Indicadores de Ponto (Bloco 5)
# ---------------------------------------------------------------------------

@app.get("/colaborador/<int:collab_id>/painel")
@ponto_required
def colaborador_painel(collab_id: int):
    """Redireciona para o histórico unificado (painel foi integrado ao histórico)."""
    sess_id = _flask_session.get("ponto_collab_id")
    if not current_user.is_authenticated and sess_id != collab_id:
        flash("Acesso não autorizado.", "danger")
        return redirect(url_for("ponto_camera"))
    month = request.args.get("month", "")
    return redirect(url_for("collaborator_history", collaborator_id=collab_id, month=month))


@app.route("/colaborador/<int:collab_id>/desconto-extra", methods=["GET", "POST"])
@ponto_required
def colaborador_desconto_extra(collab_id: int):
    if request.method == "GET":
        return redirect(url_for("collaborator_history", collaborator_id=collab_id))
    sess_id = _flask_session.get("ponto_collab_id")
    if not current_user.is_authenticated and sess_id != collab_id:
        flash("Acesso não autorizado.", "danger")
        return redirect(url_for("ponto_camera"))

    collab = db.session.get(Collaborator, collab_id)
    if not collab:
        flash("Colaborador não encontrado.", "danger")
        return redirect(url_for("ponto_camera"))

    horas_raw = (request.form.get("horas") or "").strip().replace(",", ".")
    obs = (request.form.get("obs") or "Desconto de horas extras").strip()
    month_param = request.form.get("month", "")

    try:
        horas = float(horas_raw)
        if horas <= 0:
            raise ValueError
    except (ValueError, AttributeError):
        flash("Informe um valor de horas válido (ex: 2 ou 1.5).", "warning")
        return redirect(
            url_for("colaborador_painel", collab_id=collab_id, month=month_param)
        )

    minutos_solicitados = round(horas * 60)
    ind = _calc_ponto_indicadores(collab_id)  # saldo acumulado
    saldo_min = ind["extra_saldo_min"]

    if minutos_solicitados > saldo_min:
        flash(
            f"Saldo insuficiente. Saldo R$ Extra disponível: "
            f"{_fmt_min_hhmm(saldo_min)}h. Solicitado: {_fmt_min_hhmm(minutos_solicitados)}h.",
            "danger",
        )
        return redirect(
            url_for("colaborador_painel", collab_id=collab_id, month=month_param)
        )

    criado_por = "admin" if current_user.is_authenticated else "colaborador"
    db.session.add(
        PontoAjuste(
            collaborator_id=collab_id,
            tipo="desconto_extra",
            minutos=minutos_solicitados,
            obs=obs,
            criado_por=criado_por,
        )
    )
    db.session.commit()
    flash(
        f"Desconto de {_fmt_min_hhmm(minutos_solicitados)}h registrado com sucesso.",
        "success",
    )
    return redirect(
        url_for("colaborador_painel", collab_id=collab_id, month=month_param)
    )


@app.route("/colaborador/<int:collab_id>/usar-folga-ponto", methods=["GET", "POST"])
@ponto_required
def colaborador_usar_folga_ponto(collab_id: int):
    if request.method == "GET":
        return redirect(url_for("collaborator_history", collaborator_id=collab_id))
    sess_id = _flask_session.get("ponto_collab_id")
    if not current_user.is_authenticated and sess_id != collab_id:
        flash("Acesso não autorizado.", "danger")
        return redirect(url_for("ponto_camera"))

    collab = db.session.get(Collaborator, collab_id)
    if not collab:
        flash("Colaborador não encontrado.", "danger")
        return redirect(url_for("ponto_camera"))

    date_raw = (request.form.get("data_referencia") or "").strip()
    obs = (request.form.get("obs") or "Folga utilizada").strip()
    month_param = request.form.get("month", "")

    try:
        data_ref = datetime.strptime(date_raw, "%Y-%m-%d").date()
    except ValueError:
        flash("Data inválida.", "warning")
        return redirect(
            url_for("colaborador_painel", collab_id=collab_id, month=month_param)
        )

    ind = _calc_ponto_indicadores(collab_id)  # saldo acumulado
    saldo_min = ind["folga_bruto_saldo_min"]

    if saldo_min < JORNADA_MIN:
        flash(
            f"Saldo insuficiente. Você precisa de pelo menos 7:20h (1 dia) de folga. "
            f"Saldo atual: {_fmt_min_hhmm(saldo_min)}h.",
            "danger",
        )
        return redirect(
            url_for("colaborador_painel", collab_id=collab_id, month=month_param)
        )

    criado_por = "admin" if current_user.is_authenticated else "colaborador"
    db.session.add(
        PontoAjuste(
            collaborator_id=collab_id,
            tipo="uso_folga",
            minutos=JORNADA_MIN,
            data_referencia=data_ref,
            obs=obs,
            criado_por=criado_por,
        )
    )
    db.session.commit()
    saldo_restante = saldo_min - JORNADA_MIN
    flash(
        f"Folga de {data_ref.strftime('%d/%m/%Y')} registrada. "
        f"Saldo restante: {_fmt_min_hhmm(saldo_restante)}h "
        f"({saldo_restante // JORNADA_MIN} dia(s) completo(s)).",
        "success",
    )
    return redirect(
        url_for("colaborador_painel", collab_id=collab_id, month=month_param)
    )


@app.route("/colaborador/<int:collab_id>/whatsapp", methods=["GET", "POST"])
@ponto_required
def colaborador_salvar_whatsapp(collab_id: int):
    if request.method == "GET":
        return redirect(url_for("colaborador_painel", collab_id=collab_id))
    """Salva ou limpa o número de WhatsApp do próprio colaborador."""
    sess_id = _flask_session.get("ponto_collab_id")
    if not current_user.is_authenticated and sess_id != collab_id:
        flash("Acesso não autorizado.", "danger")
        return redirect(url_for("ponto_camera"))

    collab = db.session.get(Collaborator, collab_id)
    if not collab:
        flash("Colaborador não encontrado.", "danger")
        return redirect(url_for("ponto_camera"))

    month_param = request.form.get("month", "")
    numero_raw = (request.form.get("whatsapp") or "").strip()
    # Mantém apenas dígitos
    numero = re.sub(r"\D", "", numero_raw)
    if numero and len(numero) < 10:
        flash("Número inválido. Informe DDD + número (ex: 11999999999).", "warning")
        return redirect(url_for("colaborador_painel", collab_id=collab_id, month=month_param))

    numero_anterior = collab.whatsapp
    collab.whatsapp = numero if numero else None
    db.session.commit()
    if numero:
        flash(f"WhatsApp {numero_raw} salvo. Você receberá notificações de ponto.", "success")
        if numero != numero_anterior:
            wz.boas_vindas_whatsapp(collab.name, numero)
    else:
        flash("Número de WhatsApp removido.", "info")
    return redirect(url_for("colaborador_painel", collab_id=collab_id, month=month_param))


@app.route("/colaborador/<int:collab_id>/whatsapp/teste", methods=["GET", "POST"])
@ponto_required
def colaborador_whatsapp_teste(collab_id: int):
    if request.method == "GET":
        return redirect(url_for("collaborator_history", collaborator_id=collab_id))
    sess_id = _flask_session.get("ponto_collab_id")
    if not current_user.is_authenticated and sess_id != collab_id:
        flash("Acesso não autorizado.", "danger")
        return redirect(url_for("ponto_camera"))

    collab = db.session.get(Collaborator, collab_id)
    if not collab or not collab.whatsapp:
        flash("Nenhum número cadastrado.", "warning")
        return redirect(url_for("colaborador_painel", collab_id=collab_id))

    month_param = request.form.get("month", "")
    wz.send(
        f"✅ *Teste de conexão — MultiMax*\n"
        f"Olá, {collab.name}! Sua notificação de ponto está configurada corretamente. "
        f"Número registrado: {collab.whatsapp}",
        origin="ponto_teste",
        para=collab.whatsapp,
    )
    flash(f"Mensagem de teste enviada para {collab.whatsapp}.", "success")
    return redirect(url_for("colaborador_painel", collab_id=collab_id, month=month_param))


@app.route("/colaborador/<int:collab_id>/schedule", methods=["GET", "POST"])
@ponto_required
def colaborador_salvar_schedule(collab_id: int):
    """Salva os horários de trabalho definidos pelo colaborador."""
    if request.method == "GET":
        return redirect(url_for("collaborator_history", collaborator_id=collab_id))
    sess_id = _flask_session.get("ponto_collab_id")
    if not current_user.is_authenticated and sess_id != collab_id:
        flash("Acesso não autorizado.", "danger")
        return redirect(url_for("ponto_camera"))
    collab = db.session.get(Collaborator, collab_id)
    if not collab:
        flash("Colaborador não encontrado.", "danger")
        return redirect(url_for("ponto_camera"))
    month_param = request.form.get("month", "")
    turnos = []
    for i in range(5):
        entrada = (request.form.get(f"entrada_{i}") or "").strip()
        if not entrada:
            break
        turno: dict = {"entrada": entrada}
        for campo in ("saida_intervalo", "volta_intervalo", "saida_final"):
            val = (request.form.get(f"{campo}_{i}") or "").strip()
            if val:
                turno[campo] = val
        turnos.append(turno)
    collab.schedule_json = json.dumps({"turnos": turnos}, ensure_ascii=False) if turnos else None
    db.session.commit()
    flash("Horários salvos com sucesso.", "success")
    return redirect(url_for("colaborador_painel", collab_id=collab_id, month=month_param))


@app.route("/colaborador/<int:collab_id>/alterar-senha", methods=["GET", "POST"])
@ponto_required
def colaborador_alterar_senha(collab_id: int):
    if request.method == "GET":
        return redirect(url_for("collaborator_history", collaborator_id=collab_id))
    sess_id = _flask_session.get("ponto_collab_id")
    if not current_user.is_authenticated and sess_id != collab_id:
        flash("Acesso não autorizado.", "danger")
        return redirect(url_for("ponto_camera"))
    collab = db.session.get(Collaborator, collab_id)
    if not collab:
        flash("Colaborador não encontrado.", "danger")
        return redirect(url_for("ponto_camera"))

    month_param = request.form.get("month", "")
    senha_atual = (request.form.get("senha_atual") or "").strip()
    senha_nova = (request.form.get("senha_nova") or "").strip()
    senha_conf = (request.form.get("senha_conf") or "").strip()

    if not collab.ponto_password_hash or not check_password_hash(collab.ponto_password_hash, senha_atual):
        flash("Senha atual incorreta.", "danger")
        return redirect(url_for("colaborador_painel", collab_id=collab_id, month=month_param))
    if len(senha_nova) < 4:
        flash("A nova senha deve ter pelo menos 4 caracteres.", "danger")
        return redirect(url_for("colaborador_painel", collab_id=collab_id, month=month_param))
    if senha_nova != senha_conf:
        flash("A nova senha e a confirmação não conferem.", "danger")
        return redirect(url_for("colaborador_painel", collab_id=collab_id, month=month_param))

    collab.ponto_password_hash = generate_password_hash(senha_nova)
    db.session.commit()
    flash("Senha alterada com sucesso.", "success")
    return redirect(url_for("colaborador_painel", collab_id=collab_id, month=month_param))


@app.get("/api/ponto/indicadores/<int:collab_id>")
@ponto_required
def api_ponto_indicadores(collab_id: int):
    """Retorna indicadores de ponto em JSON (para uso via AJAX)."""
    sess_id = _flask_session.get("ponto_collab_id")
    if not current_user.is_authenticated and sess_id != collab_id:
        return jsonify({"error": "não autorizado"}), 403

    year, month = _parse_month_param(request.args.get("month"))
    ind = _calc_ponto_indicadores(collab_id, year, month)
    meta_min = _calc_meta_mensal(year, month)
    h_cumprido = ind["h_normais_min"] + ind["folga_bruto_min"]

    _api_ms = date(year, month, 1)
    _api_me = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    _api_folgas = PontoAjuste.query.filter(
        PontoAjuste.collaborator_id == collab_id,
        PontoAjuste.tipo == "uso_folga",
        PontoAjuste.data_referencia >= _api_ms,
        PontoAjuste.data_referencia < _api_me,
    ).all()
    _api_folgas_uteis_min = sum(
        JORNADA_MIN for a in _api_folgas
        if a.data_referencia and not is_folga_ou_domingo(a.data_referencia)
    )

    return jsonify(
        {
            "h_bruto": _fmt_min_hhmm(ind["h_bruto_min"]),
            "h_normais": _fmt_min_hhmm(ind["h_normais_min"]),
            "folga_bruto_horas": _fmt_min_hhmm(ind["folga_bruto_saldo_min"]),
            "folga_bruto_dias": round(ind["folga_bruto_dias"], 2),
            "extra_saldo_horas": _fmt_min_hhmm(ind["extra_saldo_min"]),
            "r_extra_valor": float(ind["r_extra_valor"]),
            "dias_incompletos": ind["dias_incompletos"],
            "meta_mensal": _fmt_min_hhmm(meta_min),
            "faltantes": _fmt_min_hhmm(max(0, meta_min - h_cumprido - _api_folgas_uteis_min)),
        }
    )


# ---------------------------------------------------------------------------
# Feriados nacionais do Brasil — pré-carga 2026
# ---------------------------------------------------------------------------

_FERIADOS_BRASIL_2026: list[tuple[str, str]] = [
    ("2026-01-01", "Ano Novo"),
    ("2026-02-16", "Carnaval (Segunda-feira)"),
    ("2026-02-17", "Carnaval (Terça-feira)"),
    ("2026-04-03", "Sexta-feira Santa"),
    ("2026-04-21", "Tiradentes"),
    ("2026-05-01", "Dia do Trabalho"),
    ("2026-06-04", "Corpus Christi"),
    ("2026-09-07", "Independência do Brasil"),
    ("2026-10-12", "Nossa Senhora Aparecida"),
    ("2026-11-02", "Finados"),
    ("2026-11-15", "Proclamação da República"),
    ("2026-11-20", "Consciência Negra"),
    ("2026-12-25", "Natal"),
]


def _populate_feriados_2026() -> int:
    """Insere feriados nacionais de 2026 que ainda não existem no banco."""
    from datetime import date as _date
    added = 0
    for date_str, descricao in _FERIADOS_BRASIL_2026:
        d = _date.fromisoformat(date_str)
        if not Holiday.query.filter_by(holiday_date=d).first():
            db.session.add(Holiday(holiday_date=d, descricao=descricao))
            added += 1
    if added:
        db.session.commit()
    return added


with app.app_context():
    db.create_all()
    ensure_schema()
    ensure_admin()
    _populate_feriados_2026()


# ---------------------------------------------------------------------------
# Admin — Status do Sistema e Backups
# ---------------------------------------------------------------------------

BACKUP_DIR = os.path.join(DB_DIR, "backups")
os.makedirs(BACKUP_DIR, exist_ok=True)
MAX_BACKUPS = 3


def _do_backup() -> str:
    """Copia o banco para .db/backups/, mantendo no máximo MAX_BACKUPS arquivos."""
    import shutil
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(BACKUP_DIR, f"fluxos_zero_{ts}.db")
    shutil.copy2(DB_PATH, dest)

    # Remove backups excedentes (mais antigos primeiro)
    backups = sorted(
        [f for f in os.listdir(BACKUP_DIR) if f.endswith(".db")],
        key=lambda f: os.path.getmtime(os.path.join(BACKUP_DIR, f)),
    )
    for old in backups[:-MAX_BACKUPS]:
        try:
            os.remove(os.path.join(BACKUP_DIR, old))
        except OSError:
            pass
    return dest


@app.get("/admin/sistema")
@login_required
def admin_sistema():
    """Página de status do sistema (CPU, RAM, disco) e lista de backups."""
    import shutil
    try:
        import psutil  # type: ignore[import-untyped]
        cpu_pct   = psutil.cpu_percent(interval=0.5)
        mem       = psutil.virtual_memory()
        ram_total = mem.total
        ram_used  = mem.used
        ram_pct   = mem.percent
        disk      = psutil.disk_usage("/")
        disk_total = disk.total
        disk_used  = disk.used
        disk_pct   = disk.percent
        psutil_ok  = True
    except ImportError:
        cpu_pct = ram_total = ram_used = ram_pct = 0
        disk_total = disk_used = disk_pct = 0
        psutil_ok = False

    # Tamanho do banco principal
    db_size = os.path.getsize(DB_PATH) if os.path.isfile(DB_PATH) else 0

    # Lista de backups
    backups = []
    for fname in sorted(os.listdir(BACKUP_DIR), reverse=True):
        if not fname.endswith(".db"):
            continue
        fpath = os.path.join(BACKUP_DIR, fname)
        backups.append({
            "name": fname,
            "size": os.path.getsize(fpath),
            "mtime": datetime.fromtimestamp(os.path.getmtime(fpath)),
        })

    def fmt_bytes(n: int) -> str:
        for unit in ("B", "KB", "MB", "GB"):
            if n < 1024:
                return f"{n:.1f} {unit}"
            n /= 1024  # type: ignore[assignment]
        return f"{n:.1f} TB"

    return render_template(
        "sistema.html",
        cpu_pct=cpu_pct,
        ram_total=fmt_bytes(ram_total),
        ram_used=fmt_bytes(ram_used),
        ram_pct=ram_pct,
        disk_total=fmt_bytes(disk_total),
        disk_used=fmt_bytes(disk_used),
        disk_pct=disk_pct,
        psutil_ok=psutil_ok,
        db_size=fmt_bytes(db_size),
        backups=backups,
        fmt_bytes=fmt_bytes,
    )


@app.route("/admin/sistema/backup", methods=["GET", "POST"])
@login_required
def admin_sistema_backup():
    """Dispara um backup manual imediato."""
    try:
        dest = _do_backup()
        flash(f"Backup criado: {os.path.basename(dest)}", "success")
    except Exception as exc:  # noqa: BLE001
        flash(f"Erro ao criar backup: {exc}", "danger")
    return redirect(url_for("admin_sistema"))


@app.get("/api/sistema/status")
@login_required
def api_sistema_status():
    """Retorna métricas atuais de CPU/RAM/disco em JSON."""
    def fmt_bytes(n: int) -> str:
        for unit in ("B", "KB", "MB", "GB"):
            if n < 1024:
                return f"{n:.1f} {unit}"
            n /= 1024  # type: ignore[assignment]
        return f"{n:.1f} TB"

    try:
        import psutil  # type: ignore[import-untyped]
        cpu_pct   = psutil.cpu_percent(interval=0.5)
        mem       = psutil.virtual_memory()
        disk      = psutil.disk_usage("/")
        data = {
            "cpu_pct":    cpu_pct,
            "ram_pct":    mem.percent,
            "ram_used":   fmt_bytes(mem.used),
            "ram_total":  fmt_bytes(mem.total),
            "disk_pct":   disk.percent,
            "disk_used":  fmt_bytes(disk.used),
            "disk_total": fmt_bytes(disk.total),
            "db_size":    fmt_bytes(os.path.getsize(DB_PATH)) if os.path.isfile(DB_PATH) else "—",
        }
    except ImportError:
        data = {"error": "psutil não instalado"}
    return jsonify(data)


if __name__ == "__main__":
    host = os.getenv("FLUXOS_HOST", "0.0.0.0")
    port_raw = os.getenv("FLUXOS_PORT", "5051")
    debug_raw = os.getenv("FLUXOS_DEBUG", "0")

    try:
        port = int(port_raw)
    except ValueError:
        port = 5051

    debug = debug_raw.strip().lower() in {"1", "true", "yes", "on"}

    def _upload_cleanup_loop(interval: int = 86_400, max_age: int = 86_400) -> None:
        """Remove imagens de uploads/ponto com mais de max_age segundos, a cada interval s."""
        while True:
            try:
                now = time.time()
                removed = 0
                if os.path.isdir(UPLOAD_FOLDER):
                    for fname in os.listdir(UPLOAD_FOLDER):
                        fpath = os.path.join(UPLOAD_FOLDER, fname)
                        if not os.path.isfile(fpath):
                            continue
                        if os.path.splitext(fname)[1].lower() not in _ALLOWED_IMAGE_EXTS:
                            continue
                        if now - os.path.getmtime(fpath) > max_age:
                            try:
                                os.remove(fpath)
                                removed += 1
                            except OSError:
                                pass
                if removed:
                    app.logger.info("[cleanup] %d imagem(ns) de ponto removida(s).", removed)
            except Exception as exc:  # noqa: BLE001
                app.logger.warning("[cleanup] erro: %s", exc)
            time.sleep(interval)

    _t = threading.Thread(target=_upload_cleanup_loop, daemon=True, name="upload-cleanup")
    _t.start()

    def _alert_loop(interval: int = 60) -> None:
        """Dispara lembretes de ponto agendados quando o tempo previsto + 20min passa."""
        while True:
            time.sleep(interval)
            now = time.time()
            to_fire = []
            with _pending_alerts_lock:
                fire_keys = [k for k, v in _pending_alerts.items() if v["fire_at"] <= now]
                for k in fire_keys:
                    to_fire.append(_pending_alerts.pop(k))
            for alert in to_fire:
                try:
                    wz.lembrete_saida(
                        alert["collab_name"],
                        alert["data_str"],
                        alert["expected_time"],
                        alert["tipo"],
                        para=alert["whatsapp"],
                    )
                    app.logger.info(
                        "[alert] lembrete %s enviado para %s",
                        alert["tipo"], alert["collab_name"],
                    )
                except Exception as exc:  # noqa: BLE001
                    app.logger.warning("[alert] erro ao enviar lembrete: %s", exc)

    _ta = threading.Thread(target=_alert_loop, daemon=True, name="alert-loop")
    _ta.start()

    def _backup_loop() -> None:
        """Executa backup diário do banco de dados às 03:00."""
        while True:
            now = datetime.now()
            # Próxima execução às 03:00
            next_run = now.replace(hour=3, minute=0, second=0, microsecond=0)
            if next_run <= now:
                next_run = next_run.replace(day=now.day + 1)
            time.sleep((next_run - now).total_seconds())
            try:
                dest = _do_backup()
                app.logger.info("[backup] backup criado: %s", dest)
            except Exception as exc:  # noqa: BLE001
                app.logger.warning("[backup] erro no backup diário: %s", exc)

    _tb = threading.Thread(target=_backup_loop, daemon=True, name="backup-loop")
    _tb.start()

    app.run(host=host, debug=debug, port=port)
