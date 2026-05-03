"""Aplicacao Flask para controle de fluxos de horas por colaborador."""
# pyright: reportMissingTypeStubs=false
# pylint: disable=no-member

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import os
import uuid
from typing import Any, ClassVar, cast

import calendar

MESES_PT = ['', 'Janeiro', 'Fevereiro', 'Março', 'Abril', 'Maio', 'Junho',
            'Julho', 'Agosto', 'Setembro', 'Outubro', 'Novembro', 'Dezembro']

from functools import wraps
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
from dotenv import load_dotenv
from flask_sqlalchemy import SQLAlchemy
import notify as wz
import ponto_ocr
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import func, text
from werkzeug.security import check_password_hash, generate_password_hash


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

app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["SECRET_KEY"] = os.getenv("FLUXOS_SECRET", "trocar-esta-chave")
app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{DB_PATH}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["MAX_CONTENT_LENGTH"] = 15 * 1024 * 1024  # 15 MB


db = SQLAlchemy(model_class=Base)
db.init_app(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"
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
    """Converta string de horas em Decimal aceitando virgula ou ponto."""
    try:
        value = Decimal(raw.replace(",", ".").strip())
    except (InvalidOperation, AttributeError):
        raise ValueError("Valor de horas invalido.")
    return value


def get_setting(key: str, default: str = "") -> str:
    """Retorna o valor de uma configuracao persistida no banco."""
    s = db.session.get(Setting, key)
    return s.value if s else default


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
        hours_value = Decimal(entry.hours)
        if hours_value >= 0:
            bucket["positive"] += hours_value
        else:
            bucket["negative"] += abs(hours_value)
        bucket["net"] = bucket["positive"] - bucket["negative"]
        bucket["days"] = int(max(bucket["net"], Decimal("0")) * 60 // 440)

    cards = sorted(grouped.values(), key=lambda item: item["name"].lower())

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
    # entries for the selected month
    if month == 12:
        month_end = date(year + 1, 1, 1)
    else:
        month_end = date(year, month + 1, 1)
    entries = (
        HourEntry.query.join(
            Collaborator,
            Collaborator.id == HourEntry.collaborator_id,
        )
        .filter(
            HourEntry.entry_date >= date(year, month, 1),
            HourEntry.entry_date < month_end,
        )
        .order_by(HourEntry.entry_date.desc(), HourEntry.id.desc())
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


@app.post("/logout")
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


@app.post("/settings/admins/create")
@login_required
def admin_create():
    """Cria um novo usuario administrador."""
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


@app.post("/settings/admins/<int:user_id>/delete")
@login_required
def admin_delete(user_id: int):
    """Remove um usuario administrador (nao pode remover a si mesmo)."""
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


@app.post("/settings/daily-rate")
@login_required
def set_daily_rate():
    """Persiste o valor da diaria (7h20) usado para calculo de custo."""
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


@app.post("/collaborators")
@login_required
def create_collaborator():
    """Cria um novo colaborador ativo."""
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


@app.post("/collaborators/<int:collaborator_id>/toggle")
@login_required
def toggle_collaborator(collaborator_id: int):
    """Ativa ou desativa um colaborador existente."""
    collaborator = db.session.get(Collaborator, collaborator_id)
    if not collaborator:
        flash("Colaborador nao encontrado.", "danger")
        return redirect(url_for("index"))

    collaborator.active = not collaborator.active
    db.session.commit()
    wz.colaborador_toggle(collaborator.name, collaborator.active)
    flash("Status do colaborador atualizado.", "success")
    return redirect(url_for("index"))


@app.post("/collaborators/<int:collaborator_id>/update")
@login_required
def update_collaborator(collaborator_id: int):
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


@app.post("/entries")
@login_required
def create_entry():
    """Registra um novo lancamento de horas para um colaborador."""
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


@app.post("/entries/<int:entry_id>/update")
@login_required
def update_entry(entry_id: int):
    """Atualiza data, horas e observacao de um lancamento."""
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


@app.post("/entries/<int:entry_id>/delete")
@login_required
def delete_entry(entry_id: int):
    """Remove um lancamento existente."""
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


@app.post("/collaborators/<int:collaborator_id>/use-folga")
@login_required
def use_folga(collaborator_id: int):
    """Desconta 1 dia de folga do colaborador e cria lancamento negativo de 7h20."""
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


@app.get("/collaborators/<int:collaborator_id>/history")
def collaborator_history(collaborator_id: int):
    """Exibe o historico completo de lancamentos de um colaborador."""
    collab = db.session.get(Collaborator, collaborator_id)
    if not collab:
        flash("Colaborador nao encontrado.", "danger")
        return redirect(url_for("index"))

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
    )


# ---------------------------------------------------------------------------
# Arquivo morto
# ---------------------------------------------------------------------------

@app.post("/archive/month")
@login_required
def archive_month():
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


@app.post("/archive/month/restore")
@login_required
def restore_month():
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


@app.post("/whatsapp/resumo")
@login_required
def whatsapp_resumo():
    """Dispara o resumo geral dos colaboradores do mes para o WhatsApp."""
    year, month = _parse_month_param(request.form.get("month"))
    cards, totals = monthly_summary(year, month)
    if not cards:
        flash("Sem dados no mes selecionado para enviar.", "warning")
        return redirect(url_for("index", month=f"{year}-{month:02d}"))
    wz.resumo_geral(cards, totals)
    flash("Resumo enviado para o grupo WhatsApp.", "success")
    return redirect(url_for("index", month=f"{year}-{month:02d}"))


@app.post("/whatsapp/pdf")
@login_required
def whatsapp_pdf():
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

        pdf_bytes = HTML(string=html, base_url=request.host_url).write_pdf()
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


@app.post("/ponto/logout-ponto")
def ponto_logout():
    """Encerra a sessao do colaborador de ponto."""
    nome = _flask_session.pop("ponto_collab_name", "")
    _flask_session.pop("ponto_collab_id", None)
    flash(f"Ate logo, {nome}!" if nome else "Sessao encerrada.", "info")
    return redirect(url_for("ponto_login"))


@app.get("/api/suggest-password")
@login_required
def api_suggest_password():
    """Retorna senha sugerida para um colaborador (admin only)."""
    name = (request.args.get("name") or "").strip()
    if not name:
        return jsonify({"error": "nome obrigatorio"}), 400
    return jsonify({"senha": suggest_ponto_password(name)})


@app.post("/collaborators/<int:collaborator_id>/set-ponto-password")
@login_required
def set_ponto_password(collaborator_id: int):
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


@app.post("/collaborators/<int:collaborator_id>/make-admin")
@login_required
def make_collaborator_admin(collaborator_id: int):
    """Cria uma conta de administrador para o colaborador."""
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

    return render_template(
        "ponto_confirmar.html",
        data=data,
        cpf_clean=cpf_clean,
        filename=filename,
        collab=collab,
        collaborators=collaborators,
        existing_count=existing_count,
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

    return redirect(url_for("ponto"))


@app.post("/ponto/<int:record_id>/vincular")
@login_required
def ponto_vincular(record_id: int):
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


@app.post("/ponto/<int:record_id>/delete")
@login_required
def ponto_delete(record_id: int):
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


@app.route("/sw.js")
def service_worker():
    """Serve o Service Worker com escopo raiz permitido."""
    response = make_response(
        app.send_static_file("sw.js")
    )
    response.headers["Service-Worker-Allowed"] = "/"
    response.headers["Content-Type"] = "application/javascript"
    return response


with app.app_context():
    db.create_all()
    ensure_schema()
    ensure_admin()


if __name__ == "__main__":
    host = os.getenv("FLUXOS_HOST", "0.0.0.0")
    port_raw = os.getenv("FLUXOS_PORT", "5051")
    debug_raw = os.getenv("FLUXOS_DEBUG", "0")

    try:
        port = int(port_raw)
    except ValueError:
        port = 5051

    debug = debug_raw.strip().lower() in {"1", "true", "yes", "on"}
    app.run(host=host, debug=debug, port=port)
