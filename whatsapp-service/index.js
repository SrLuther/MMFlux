/**
 * whatsapp-service/index.js
 *
 * Serviço WhatsApp do MMFlux.
 *
 * Endpoints:
 *   POST /notify  { mensagem, origin, para? }
 *     - sem "para" → envia para o grupo Notify
 *     - com "para" → envia para o número individual
 *
 *   GET  /health          → status geral
 *   GET  /health/whatsapp → status da conexão
 *   GET  /qr.png          → QR Code para autenticação (PNG)
 */

// Compatibilidade com Node 18
if (!global.crypto) {
  const { webcrypto } = require("crypto");
  global.crypto = webcrypto;
}

const path = require("path");
const express = require("express");
const qrcode = require("qrcode-terminal");
const qrcodePng = require("qrcode");
const {
  default: makeWASocket,
  DisconnectReason,
  fetchLatestBaileysVersion,
  useMultiFileAuthState,
} = require("@whiskeysockets/baileys");

const { sendEvent, validatePhoneNumber, formatPhoneForWhatsApp } = require("./notify");

// ── Logger simples (JSON para journald) ─────────────────────────────────────

const createLogger = (module) => {
  const levels = { trace: 10, debug: 20, info: 30, warn: 40, error: 50, fatal: 60 };
  const writeLog = (levelName, dataOrMsg, maybeMsg) => {
    let payload = {};
    let msg = "";
    if (typeof dataOrMsg === "string") {
      msg = dataOrMsg;
    } else if (dataOrMsg && typeof dataOrMsg === "object") {
      payload = { ...dataOrMsg };
      if (payload.err instanceof Error) {
        payload.err = { message: payload.err.message, stack: payload.err.stack };
      }
      msg = typeof maybeMsg === "string" ? maybeMsg : "";
    }
    const entry = JSON.stringify({ level: levels[levelName] ?? 30, time: Date.now(), module, ...payload, msg });
    levels[levelName] >= 50 ? console.error(entry) : console.log(entry);
  };
  const logger = {};
  for (const l of ["trace", "debug", "info", "warn", "error", "fatal"]) {
    logger[l] = (a, b) => writeLog(l, a, b);
  }
  logger.child = () => createLogger(module);
  return logger;
};

const logger = createLogger("mmflux-whatsapp");

// ── Estado global ────────────────────────────────────────────────────────────

let globalSocket = null;
let notifyGroupId = null;
let baileysConnected = false;
let reconnectTimeout = null;
let startupAlertSent = false;
let lastQr = null;
let lastQrAt = null;
let lastQrPng = null;

// Cache de mensagens para habilitar retentativas de entrega (getMessage)
const msgCache = new Map();
const MSG_CACHE_MAX = 500;
function cacheMessage(key, msg) {
  const id = key?.id || JSON.stringify(key);
  msgCache.set(id, msg);
  if (msgCache.size > MSG_CACHE_MAX) {
    msgCache.delete(msgCache.keys().next().value);
  }
}

// ── Envio para o grupo Notify ────────────────────────────────────────────────

async function sendToNotifyGroup(mensagem, arquivoBase64, nomeArquivo) {
  if (!globalSocket) throw new Error("WhatsApp não está conectado");
  if (!notifyGroupId) throw new Error("Grupo 'Notify' não encontrado");

  const messageContent = {};
  if (mensagem) messageContent.text = mensagem;
  if (arquivoBase64 && nomeArquivo) {
    messageContent.document = Buffer.from(arquivoBase64, "base64");
    messageContent.fileName = nomeArquivo;
    messageContent.mimetype = "application/pdf";
  }

  try {
    await globalSocket.sendMessage(notifyGroupId, messageContent);
    logger.info({ grupo: "Notify", tamanho: mensagem?.length || 0 }, "Mensagem enviada ao grupo");
    return true;
  } catch (err) {
    if (err.message?.includes("history")) { logger.warn({ err: err.message }, "Aviso de histórico ignorado"); return true; }
    logger.error({ err, grupo: "Notify" }, "Falha ao enviar para grupo");
    throw err;
  }
}

// ── Envio para número individual ─────────────────────────────────────────────

async function sendToIndividual(para, mensagem, arquivoBase64, nomeArquivo) {
  if (!globalSocket) throw new Error("WhatsApp não está conectado");

  const jid = formatPhoneForWhatsApp(para);
  if (!jid) throw new Error(`Número inválido: ${para}`);

  // Verificar e resolver o JID canônico (lida com números com/sem dígito 9)
  let resolvedJid = jid;
  try {
    const [result] = await globalSocket.onWhatsApp(jid);
    if (!result?.exists) {
      const numerico = jid.replace("@s.whatsapp.net", "");
      let altNum = null;
      if (numerico.length === 13 && numerico.startsWith("55")) {
        altNum = numerico.slice(0, 4) + numerico.slice(5); // remove dígito 9
      } else if (numerico.length === 12 && numerico.startsWith("55")) {
        altNum = numerico.slice(0, 4) + "9" + numerico.slice(4); // adiciona dígito 9
      }
      if (altNum) {
        const altJid = `${altNum}@s.whatsapp.net`;
        const [altResult] = await globalSocket.onWhatsApp(altJid);
        if (altResult?.exists) {
          logger.info({ original: jid, resolvido: altJid }, "JID alternativo encontrado");
          resolvedJid = altJid;
        } else {
          logger.warn({ jid }, "Número não encontrado no WhatsApp — enviando assim mesmo");
        }
      } else {
        logger.warn({ jid }, "Número não encontrado no WhatsApp — enviando assim mesmo");
      }
    } else {
      resolvedJid = result.jid || jid;
    }
  } catch (checkErr) {
    logger.warn({ err: checkErr.message, jid }, "Não foi possível verificar JID — continuando");
  }

  const messageContent = {};
  if (mensagem) messageContent.text = mensagem;
  if (arquivoBase64 && nomeArquivo) {
    messageContent.document = Buffer.from(arquivoBase64, "base64");
    messageContent.fileName = nomeArquivo;
    messageContent.mimetype = "application/pdf";
  }

  try {
    const sentMsg = await globalSocket.sendMessage(resolvedJid, messageContent);
    if (sentMsg?.key) cacheMessage(sentMsg.key, sentMsg.message);
    logger.info({ jid: resolvedJid, tamanho: mensagem?.length || 0 }, "Mensagem individual enviada");
    return true;
  } catch (err) {
    if (err.message?.includes("history")) { logger.warn({ err: err.message }, "Aviso de histórico ignorado"); return true; }
    logger.error({ err, jid: resolvedJid }, "Falha ao enviar mensagem individual");
    throw err;
  }
}

// ── Servidor HTTP ─────────────────────────────────────────────────────────────

function setupHttpServer() {
  const app = express();
  app.use(express.json());

  // Healthcheck geral
  app.get("/health", (_req, res) => {
    res.json({ status: "ok", service: "mmflux-whatsapp", connected: baileysConnected, timestamp: new Date().toISOString() });
  });

  // Status da conexão WhatsApp
  app.get("/health/whatsapp", (_req, res) => {
    res.json({ connected: baileysConnected, timestamp: new Date().toISOString() });
  });

  // QR Code em PNG (escaneie para autenticar)
  app.get("/qr.png", (_req, res) => {
    if (!lastQrPng) return res.status(404).json({ erro: "QR Code não disponível" });
    res.setHeader("Content-Type", "image/png");
    res.setHeader("Cache-Control", "no-store");
    if (lastQrAt) res.setHeader("X-QR-Generated-At", lastQrAt.toISOString());
    res.send(lastQrPng);
  });

  // Endpoint principal de notificações
  app.post("/notify", async (req, res) => {
    const { mensagem, origin, arquivo_base64, nome_arquivo, para } = req.body;

    if (!mensagem && !arquivo_base64) {
      logger.warn("Requisição /notify sem mensagem ou arquivo");
      return res.status(400).json({ erro: "Campo 'mensagem' ou 'arquivo_base64' é obrigatório" });
    }

    try {
      if (para) {
        logger.info({ origem: origin || "unknown", para }, "Processando envio individual");
        await sendToIndividual(para, mensagem, arquivo_base64, nome_arquivo);
        res.json({ sucesso: true, mensagem: `Enviado para ${para}` });
      } else {
        logger.info({ origem: origin || "unknown" }, "Processando envio para grupo Notify");
        await sendToNotifyGroup(mensagem, arquivo_base64, nome_arquivo);
        res.json({ sucesso: true, mensagem: "Enviado para grupo Notify" });
      }
    } catch (err) {
      logger.error({ err, origin }, "Erro ao processar /notify");
      res.status(500).json({ erro: err.message || "Falha ao enviar mensagem" });
    }
  });

  const PORT = parseInt(process.env.WA_PORT || "3001", 10);
  app.listen(PORT, "127.0.0.1", () => {
    logger.info({ port: PORT }, "Servidor HTTP iniciado (apenas loopback)");
  });
}

// ── Conexão WhatsApp ──────────────────────────────────────────────────────────

async function listGroups(sock) {
  try {
    const groupsMap = await sock.groupFetchAllParticipating();
    const groups = Object.values(groupsMap).sort((a, b) => (a.subject || "").localeCompare(b.subject || ""));
    logger.info("Grupos disponíveis:");
    for (const g of groups) {
      const name = g.subject || "(sem nome)";
      logger.info(`  ${name} → ${g.id}`);
      if (name.toLowerCase() === "notify") {
        notifyGroupId = g.id;
        logger.info(`✓ Grupo Notify identificado: ${g.id}`);
      }
    }
    if (!notifyGroupId) logger.warn("Grupo 'Notify' não encontrado — crie um grupo com esse nome exato no WhatsApp");
  } catch (err) {
    logger.error({ err }, "Erro ao listar grupos");
  }
}

async function connectToWhatsApp() {
  const authFolder = path.join(__dirname, "auth");
  const { state, saveCreds } = await useMultiFileAuthState(authFolder);
  const { version } = await fetchLatestBaileysVersion();

  const sock = makeWASocket({
    version,
    auth: state,
    printQRInTerminal: false,
    logger,
    markOnlineOnConnect: false,
    // Chrome é mais confiável para entrega de mensagens individuais
    browser: ["Ubuntu", "Chrome", "22.0.0"],
    // getMessage é obrigatório para o WhatsApp retentar entrega quando
    // o destinatário está offline/em background na primeira tentativa
    getMessage: async (key) => {
      const id = key?.id;
      if (id && msgCache.has(id)) return msgCache.get(id);
      return { conversation: "" };
    },
  });

  globalSocket = sock;

  sock.ev.on("creds.update", saveCreds);

  // Popula cache com todas as mensagens (enviadas e recebidas)
  sock.ev.on("messages.upsert", ({ messages }) => {
    for (const msg of messages) {
      if (msg.key) cacheMessage(msg.key, msg.message);
    }
  });

  sock.ev.on("connection.update", async (update) => {
    const { connection, lastDisconnect, qr } = update;

    if (qr) {
      logger.info("Escaneie o QR Code para conectar:");
      logger.info(`QR_CODE_DATA:${qr}`);
      const fs = require("fs");
      try { fs.writeFileSync(path.join(authFolder, "last-qr.txt"), qr, "utf8"); } catch (_) {}
      lastQr = qr;
      lastQrAt = new Date();
      try { lastQrPng = await qrcodePng.toBuffer(qr, { type: "png", margin: 1, width: 320 }); } catch (_) { lastQrPng = null; }
      qrcode.generate(qr, { small: true });
    }

    if (connection === "open") {
      logger.info("✓ Conectado ao WhatsApp");
      baileysConnected = true;
      await listGroups(sock);
      if (!startupAlertSent) {
        setTimeout(async () => {
          await sendEvent(sock, { type: "startup", level: "info", source: "mmflux-whatsapp", message: "MMFlux WhatsApp Service iniciado", force: true });
          startupAlertSent = true;
        }, 3000);
      }
    }

    if (connection === "close") {
      baileysConnected = false;
      const code = lastDisconnect?.error?.output?.statusCode;
      if (code === DisconnectReason.loggedOut) {
        logger.error("Sessão expirada — apague a pasta whatsapp-service/auth/ e reinicie o serviço");
        process.exit(1);
      } else {
        logger.warn("Conexão perdida. Reconectando em 5 segundos...");
        reconnectTimeout = setTimeout(() => connectToWhatsApp(), 5000);
      }
    }
  });

  return sock;
}

// ── Tratamento de erros não capturados ───────────────────────────────────────

process.on("unhandledRejection", (reason) => {
  logger.error({ reason: reason?.message || String(reason) }, "Unhandled Rejection");
});

process.on("uncaughtException", (error) => {
  logger.error({ err: { message: error.message, stack: error.stack } }, "Uncaught Exception");
  process.exit(1);
});

// ── Inicialização ─────────────────────────────────────────────────────────────

async function main() {
  logger.info("Iniciando MMFlux WhatsApp Service...");
  setupHttpServer();
  await connectToWhatsApp();
}

main().catch((err) => {
  logger.error({ err }, "Falha ao iniciar");
  process.exit(1);
});
