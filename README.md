# Fluxos Zero

Sistema novo e separado, criado do zero, focado somente na mecanica de Fluxos:
- cadastro de colaboradores
- registro de horas (+ e -)
- resumo mensal por colaborador
- visualizacao publica sem login
- login apenas para criar/editar/excluir dados

## Requisitos
- Python 3.10+

## Configuracao de ambiente (.env)
1. Copie `.env.example` para `.env`.
2. Defina valores reais para as variaveis:

- FLUXOS_ADMIN_USER
- FLUXOS_ADMIN_PASS
- FLUXOS_SECRET

## Como rodar
1. Criar ambiente virtual e instalar dependencias:

PowerShell:

python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

2. (Opcional) Definir usuario e senha admin via variaveis:

$env:FLUXOS_ADMIN_USER="admin"
$env:FLUXOS_ADMIN_PASS="admin123"
$env:FLUXOS_SECRET="uma-chave-secreta-forte"

3. Iniciar servidor:

python app.py

4. Acessar no navegador:
- http://127.0.0.1:5051

## Regras de acesso
- Sem login: apenas leitura (dashboard, lista de colaboradores, historico de horas)
- Com login: cadastro e atualizacao de colaboradores e lancamentos

## Estrutura
- app.py: backend Flask + modelos + rotas
- templates/: paginas HTML
- static/: estilos
- fluxos_zero.db: banco SQLite criado automaticamente

## Deploy em servidor Linux (Ubuntu/Debian)

1. Clone o repositorio na VM:
   ```bash
   git clone https://github.com/SrLuther/MMFlux.git /opt/mmflux
   ```

2. Execute o script de instalacao como root:
   ```bash
   sudo bash /opt/mmflux/deploy/install.sh
   ```

3. Edite o `.env` com seus valores reais:
   ```bash
   sudo nano /opt/mmflux/.env
   ```

4. Inicie o servico:
   ```bash
   sudo systemctl start mmflux
   sudo systemctl status mmflux
   ```

5. Para ver logs:
   ```bash
   sudo journalctl -u mmflux -f
   ```

O app roda na porta `5051`. Configure nginx ou similar para proxy reverso se necessario.

## Versionamento obrigatorio para commit/push
Este projeto possui bloqueio automatico por hooks de Git.

Regras obrigatorias antes de commit e push:
- atualizar `VERSION` no formato X.Y.Z
- registrar a versao no `CHANGELOG.md` no formato:
	`## [X.Y.Z] - AAAA-MM-DD`

Se isso nao for feito, commit e push sao bloqueados.
