# Changelog

## [0.10.1] - 2026-05-07
- fix: Service Worker reescrito — páginas HTML sempre buscadas da rede (network-only para navegação), assets estáticos usam cache-first com atualização em background. Elimina tela branca após deploys.
- chore: cache SW atualizado para `mmflux-v7`, versão CSS atualizada para `?v=7`.

## [0.10.0] - 2026-05-06
- feat: painel do colaborador — bloco de opções reformulado em card unificado com linhas separadoras e chevron animado.
- feat: colaborador pode alterar a própria senha de ponto diretamente no painel.
- feat: recuperação de senha via WhatsApp — senha temporária de 6 caracteres gerada com `secrets` e enviada ao número cadastrado.
- feat: seção "Notificações WhatsApp" no painel do colaborador — salvar/remover número e enviar mensagem de teste.
- feat: horários de trabalho em layout 2×2 (Entrada|Saída Intervalo / Volta Intervalo|Saída Final).
- feat: botão "Meu Painel" na tela de captura de ponto agora visível (cor sólida).
- feat: botões de paginação (Anterior/Próxima) na listagem de registros agora visíveis.
- feat: tela de seleção de acesso (Administrador/Colaborador) com descrições legíveis.
- chore: arquivos de desenvolvimento `seed_ponto_test.py` e `_ocr_test.py` removidos do repositório.

## [0.9.1] - 2026-05-02
- fix: nome do mês exibido em PT-BR ("Maio" em vez de "May").
- feat: seletor de meses do resumo reformulado — botões coloridos clicáveis (verde = incluso, vermelho = excluído) sem checkboxes.
- fix: botões de navegação de mês e semana agora com fundo navy sólido, visíveis em fundo claro.
- chore: CHANGELOG retroativo com entradas de v0.6.0, v0.7.0 e v0.8.0.

## [0.9.0] - 2026-05-02
- feat: painel administrativo dedicado no index — exportar PDF, enviar WhatsApp, arquivar mês, definir diária e lançar horas em bloco único visível apenas para admin.
- feat: botão "Exportar PDF" movido para fora do painel admin — disponível para qualquer visitante.
- feat: paginação semanal no histórico do colaborador (nav ← semana →).
- feat: paginação por mês no histórico do colaborador (nav ← Anterior / Próximo →).
- feat: métricas do histórico reordenadas e coloridas: H Bruto (verde), H Descontos (vermelho), neutros (amarelo), Valor Est. (verde + borda verde).
- feat: filtro "Meses no resumo" reposicionado como card no grid de métricas — abre painel inline abaixo do nome.
- feat: seção de lançamentos do histórico agrupada em card branco de largura igual ao hero.
- fix: tabelas em `.hist-content` ocultas pelo reset CSS global — override adicionado.
- fix: botões de navegação invisíveis em fundo claro — override de cor aplicado.
- fix: métricas duplicadas após refatoração — bloco residual removido.
- chore: CSS `.hist-content` refatorado para card único com border-radius e largura uniforme.

## [0.8.0] - 2026-05-02
- feat: campo `gives_folga` em HourEntry — lançamentos marcados como "Direito a Folga" acumulam dias de folga no colaborador.
- feat: modelo Collaborator ganha `folga_days` (contador de dias acumulados).
- feat: rota `POST /collaborators/<id>/use-folga` — desconta 1 dia de folga com data e observação.
- feat: barra de uso de folga no histórico do colaborador (botão desabilitado quando sem saldo).
- feat: tag 🌴 exibida nos lançamentos com folga nas tabelas e no PDF individual.
- feat: checkbox "D. Folga" no formulário de lançar horas e na confirmação de ponto.
- fix: migração automática das colunas `gives_folga` e `folga_days` em bases existentes.
- chore: CSS `.folga-tag`, `.btn-folga`, `.hist-folga-bar` adicionados.

## [0.7.0] - 2026-05-02
- feat: arquivo morto de lançamentos — rota `/archive` lista lançamentos arquivados com totais por mês.
- feat: rota `POST /archive/month` arquiva todos os lançamentos de um mês (remove do painel ativo).
- feat: PDF individual por colaborador (`/collaborators/<id>/pdf`) com todos os lançamentos e totais.
- feat: botão "Baixar PDF" na página de histórico do colaborador.
- feat: template `pdf_collab.html` com capa, sumário e lançamentos detalhados por mês.
- feat: promoção de colaborador a admin — cria login de acesso total a partir do cadastro.
- feat: gestão de admins — criar e remover usuários admin pelo painel.
- fix: `/ponto/associar-cpf` exigia `login_required` em vez de `ponto_required`.
- fix: confirmação OCR renderiza HTML direto em vez de redirecionar via URL externa.
- fix: rota `/ponto/upload` aceita GET para evitar erro 405 após OCR.

## [0.6.0] - 2026-05-02
- feat: sistema de usuários exclusivo para alimentar o ponto via câmera.
- feat: colaboradores cadastrados podem fazer login com nome + senha de ponto.
- feat: coluna `ponto_password_hash` no modelo Collaborator com migração automática.
- feat: decorator `ponto_required` — câmera/upload/confirmar exigem autenticação (admin ou colaborador).
- feat: página de login de colaborador (`/ponto/login`).
- feat: topbar exibe nome do colaborador logado com botão de sair.
- feat: função `suggest_ponto_password` — gera senha padrão por posição alfabética (ex: Luciano → L1221, Maria → M13118).
- feat: botão "Sugerir" no cadastro e edição de colaborador preenche senha automaticamente via AJAX.
- feat: rota `POST /collaborators/<id>/set-ponto-password` para admin definir/redefinir senha.
- feat: rota `GET /api/suggest-password` retorna senha sugerida para um nome.
- feat: filtro Jinja `|hhmm` — converte horas decimais para formato legível (ex: 7.38 → 7h23).
- feat: histórico de colaborador usa `|hhmm` em todos os valores de horas.
- feat: OCR via Gemini API (`gemini-flash-lite-latest`) como motor primário; Tesseract como fallback.
- chore: `.gitignore` atualizado para ignorar `uploads/` e arquivos de banco.

## [0.5.1] - 2026-05-02
- feat: fluxo de confirmação antes de registrar o ponto (página ponto_confirmar.html).
- feat: todos os campos OCR são editáveis pelo usuário antes de confirmar.
- feat: intervalo calculado automaticamente ao chegar a 2ª batida do dia (entrada/saída em qualquer ordem).
- feat: HourEntry criado automaticamente com nota "Ponto: HH:MM → HH:MM (comprovante)".
- feat: campo `processed` em PunchRecord evita duplo cálculo de intervalo.
- feat: migração automática do campo `processed` em bases existentes.
- fix: rota /ponto/uploads liberada para exibir preview do comprovante na tela de confirmação.
- fix: import `re` adicionado ao app.py.

## [0.5.0] - 2026-05-02
- feat: módulo de ponto eletrônico via OCR de comprovante fotografado.
- feat: modelo PunchRecord (data, hora, NSR, NREP, AD, CPF, colaborador).
- feat: campo `cpf` no modelo Collaborator para vinculação automática.
- feat: rota GET/POST /ponto — página mobile com captura de câmera.
- feat: deduplicação por NSR (Número Sequencial de Registro único do relógio).
- feat: vínculo manual de registros pendentes (admin).
- feat: exclusão de registros de ponto (admin).
- feat: rota /ponto/uploads/<file> para servir comprovantes (admin).
- chore: migração automática de schema (coluna cpf em collaborator).
- chore: pytesseract e Pillow adicionados ao requirements.txt.
- chore: MAX_CONTENT_LENGTH 15 MB para uploads de imagem.

## [0.4.8] - 2026-04-18
- fix: PDF não quebra mais no meio do bloco de um colaborador em "Lançamentos Detalhados" (evita corte entre páginas).

## [0.4.7] - 2026-04-18
- feat: diária individual por colaborador (cadastro e edição).
- feat: histórico agora mostra valor estimado que o colaborador receberá.
- feat: cálculo no histórico usa a diária do colaborador; fallback para diária global quando não definida.
- chore: migração automática de schema para adicionar coluna `daily_rate` em bases SQLite existentes.

## [0.4.6] - 2026-04-18
- feat: edição de colaborador também na página de histórico.
- ajuste: ao editar colaborador a partir do histórico, permanece na mesma página.
- fix: variáveis CSS de layout (--page-px e --max-w) definidas para corrigir cards encostando nas bordas no histórico.

## [0.4.5] - 2026-04-18
- feat: edicao de colaborador na lista principal (nome e funcao).
- novo endpoint POST /collaborators/<id>/update para persistir alteracoes.
- notify.py: evento de colaborador atualizado via WhatsApp.

## [0.4.4] - 2026-04-18
- fix: rodapé do PDF em duas linhas — site na primeira, número de página na segunda, ambos centralizados.

## [0.4.3] - 2026-04-18
- fix: número de página centralizado no rodapé, separado do texto da esquerda.

## [0.4.2] - 2026-04-18
- fix: remover page-break-after da capa para evitar página em branco; capa e conteúdo na mesma página.

## [0.4.1] - 2026-04-18
- fix: mover numeração de página do cabeçalho para o rodapé do PDF.

## [0.4.0] - 2026-04-18
- fix: substituir páginas nomeadas por @page/:first no PDF para eliminar página em branco no WeasyPrint.

## [0.3.9] - 2026-04-18
- fix: remover regra @page genérica que gerava página em branco antes da capa no WeasyPrint.

## [0.3.8] - 2026-04-18
- fix: remover page-break-after duplo no .cover que gerava páginas em branco no PDF.

## [0.3.7] - 2026-04-17
- Botao "Enviar Resumo" no painel principal (visivel apenas para usuarios autenticados).
- POST /whatsapp/resumo: gera resumo mensal de todos os colaboradores e envia para o grupo WhatsApp Notify.
- Mensagem formatada com nome, horas bruto/desconto/liquido e dias por colaborador + totais gerais.
- Botao verde (#25d366) ao lado do Exportar PDF, com confirmacao antes do envio.
- notify.py: funcao resumo_geral(cards, totals).

## [0.3.6] - 2026-04-17
- Pagina de historico por colaborador (/collaborators/<id>/history).
- Mostra totais globais + lancamentos agrupados por mes com resumo mensal.
- Botao Historico na lista de colaboradores (visivel para todos).
- Editar/excluir lancamento do historico redireciona de volta ao historico.
- CSS: .hist-hero, .hist-month, .mstat (pos/neg/net).

## [0.3.5] - 2026-04-17
- Notificacoes WhatsApp via servico multimax.tec.br/notify.
- Eventos: novo lancamento, atualizacao, remocao, colaborador criado/toggle.
- notify.py: gateway async (thread daemon), fallback de URL, falha silenciosa.
- requests adicionado ao requirements.txt.

## [0.3.4] - 2026-04-17
- PDF com duas secoes: resumo geral por colaborador + lancamentos detalhados.
- Capa com gradiente da marca, tabela de entradas com data/horas/observacao.

## [0.3.3] - 2026-04-17
- Renomeia sistema de Fluxos Zero/MMFlux para MultiMax nos templates, manifest, service e PDF.

## [0.3.2] - 2026-04-17
- Fix: WeasyPrint atualizado para 68.x — incompatibilidade com pydyf 0.12.1 causava erro ao gerar PDF.

## [0.3.1] - 2026-04-17
- Remove h2 desnecessario do hero.

## [0.3.0] - 2026-04-17
- Fix: botao cortado na topbar no mobile (min-height, padding, flex-shrink, logo menor).
- Texto do botao encurtado para "Entrar" na topbar.

## [0.2.9] - 2026-04-17
- Fix: CSS duplicado removido (create_file havia colado CSS antigo ao novo).
- Fix: manifest.json com purpose separados (any + maskable) para habilitar PWA install no Chrome.

## [0.2.8] - 2026-04-17
- Fix: SW cache bumped para mmflux-v2 para invalidar CSS antigo.

## [0.2.7] - 2026-04-17
- Redesign visual completo: topbar navy com gradiente, hero colorido com decoracao, cards com borda teal, tipografia hierarquica.
- Logo branca na topbar escura.
- brand-text com nome e subtitulo na topbar.

## [0.2.6] - 2026-04-17
- Corrigido topbar mobile: flex-wrap para nao sobrepor o conteudo.
- padding-top do body calculado dinamicamente via JS conforme altura real da topbar.

## [0.2.5] - 2026-04-17
- Logo atualizada para MMFx2.png.

## [0.2.4] - 2026-04-17
- Corrigido erro de Service Worker: rota /sw.js serve o arquivo com header Service-Worker-Allowed para escopo raiz.

## [0.2.3] - 2026-04-17
- Logo MMFx adicionada na topbar.
- Paleta de cores atualizada com as cores da logomarca (navy, teal, azul medio).
- Gradiente dos botoes alinhado com a identidade visual.

## [0.2.2] - 2026-04-17
- Layout mobile-first: topbar fixa, coluna unica, cards de lancamento.
- Fonte trocada para Ubuntu / Ubuntu Mono.
- Brand atualizado para MultiMax com subtitulo "Sistema simplificado".
- Configuracoes do VS Code para associacao de arquivos VERSION.

## [0.2.1] - 2026-04-17
- Ajustado startup do Flask para usar `FLUXOS_HOST`/`FLUXOS_PORT`/`FLUXOS_DEBUG`.
- Host padrao alterado para `0.0.0.0`, permitindo acesso externo na VPS.

## [0.2.0] - 2026-04-17
- Adicionado script de instalacao automatica para Linux (deploy/install.sh).
- Adicionado arquivo de servico systemd (deploy/mmflux.service).
- Instrucoes de deploy no README.

## [0.1.0] - 2026-04-17
- Versao inicial do projeto Fluxos Zero.
