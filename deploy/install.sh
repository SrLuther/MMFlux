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
    curl \
    libcairo2 \
    libpango-1.0-0 \
    libgdk-pixbuf-2.0-0 \
    libffi-dev \
    shared-mime-info \
    fonts-dejavu-core \
    tesseract-ocr \
    tesseract-ocr-por \
    libtesseract-dev

echo "==> Instalando Node.js 20 LTS..."
if ! command -v node &>/dev/null; then
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
    apt-get install -y nodejs
fi
echo "   Node.js $(node --version), npm $(npm --version)"

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

echo "==> Instalando dependencias do whatsapp-service..."
cd "$APP_DIR/whatsapp-service"
npm install --omit=dev --quiet
mkdir -p auth
chown -R "$APP_USER:$APP_USER" "$APP_DIR/whatsapp-service"
cd "$APP_DIR"

echo "==> Instalando servico systemd MMFlux..."
cp "$APP_DIR/deploy/mmflux.service" "/etc/systemd/system/mmflux.service"

echo "==> Instalando servico systemd MMFlux WhatsApp..."
cp "$APP_DIR/deploy/mmflux-whatsapp.service" "/etc/systemd/system/mmflux-whatsapp.service"

systemctl daemon-reload
systemctl enable mmflux
systemctl enable mmflux-whatsapp

echo ""
echo "Instalacao concluida."
echo "Para iniciar o MMFlux:           sudo systemctl start mmflux"
echo "Para iniciar o WhatsApp Service: sudo systemctl start mmflux-whatsapp"
echo "Para autenticar o WhatsApp:      acesse http://<ip>:3001/qr.png e escaneie o QR"
echo "Para ver logs Flask:             sudo journalctl -u mmflux -f"
echo "Para ver logs WhatsApp:          sudo journalctl -u mmflux-whatsapp -f"
