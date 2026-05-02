#!/bin/bash
# Script de instalacao do MMFlux em Ubuntu/Debian.
# Uso: sudo bash deploy/install.sh
set -euo pipefail

APP_USER="mmflux"
APP_DIR="/opt/mmflux"
REPO_URL="https://github.com/SrLuther/MMFlux.git"
SERVICE_NAME="mmflux"

echo "==> Atualizando pacotes..."
apt-get update -y

echo "==> Instalando dependencias de sistema..."
apt-get install -y \
    python3 \
    python3-venv \
    python3-pip \
    git \
    libcairo2 \
    libpango-1.0-0 \
    libgdk-pixbuf-2.0-0 \
    libffi-dev \
    shared-mime-info \
    fonts-dejavu-core \
    tesseract-ocr \
    tesseract-ocr-por \
    libtesseract-dev

echo "==> Criando usuario de servico '$APP_USER'..."
id "$APP_USER" &>/dev/null || useradd --system --shell /bin/false --home "$APP_DIR" "$APP_USER"

echo "==> Clonando repositorio em $APP_DIR..."
if [ -d "$APP_DIR/.git" ]; then
    git -C "$APP_DIR" pull --ff-only
else
    git clone "$REPO_URL" "$APP_DIR"
fi

echo "==> Criando ambiente virtual..."
python3 -m venv "$APP_DIR/.venv"

echo "==> Instalando dependencias Python..."
"$APP_DIR/.venv/bin/pip" install --upgrade pip --quiet
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt" --quiet

echo "==> Criando pasta do banco de dados..."
mkdir -p "$APP_DIR/.db"

echo "==> Configurando .env..."
if [ ! -f "$APP_DIR/.env" ]; then
    cp "$APP_DIR/.env.example" "$APP_DIR/.env"
    echo ""
    echo "ATENCAO: edite $APP_DIR/.env e defina FLUXOS_SECRET, FLUXOS_ADMIN_USER e FLUXOS_ADMIN_PASS antes de iniciar o servico."
    echo ""
fi

echo "==> Ajustando permissoes..."
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
chmod 600 "$APP_DIR/.env"

echo "==> Instalando servico systemd..."
cp "$APP_DIR/deploy/mmflux.service" "/etc/systemd/system/${SERVICE_NAME}.service"
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"

echo ""
echo "Instalacao concluida."
echo "Para iniciar: sudo systemctl start $SERVICE_NAME"
echo "Para ver logs: sudo journalctl -u $SERVICE_NAME -f"
