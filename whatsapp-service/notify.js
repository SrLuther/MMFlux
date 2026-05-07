/**
 * whatsapp-service/notify.js
 *
 * Utilitários de formatação e envio de eventos via WhatsApp (Baileys).
 * Suporta envio para grupo Notify e para números individuais.
 */

const dayjs = require("dayjs");
const utc = require("dayjs/plugin/utc");
const timezone = require("dayjs/plugin/timezone");

dayjs.extend(utc);
dayjs.extend(timezone);

// Cache anti-spam: evita reenviar eventos idênticos em menos de 5 minutos
const eventCache = { lastHash: null, lastSent: null, ttl: 5 * 60 * 1000 };

function hashEvent(event) {
  const key = `${event.type}|${event.level}|${event.source}|${event.message}`;
  return require("crypto").createHash("md5").update(key).digest("hex");
}

/**
 * Limpa e valida número de telefone brasileiro.
 * Retorna string com apenas dígitos e prefixo 55, ou null se inválido.
 */
function validatePhoneNumber(phone) {
  if (!phone || typeof phone !== "string") return null;
  let clean = phone.replace(/\D/g, "");
  if (!clean.startsWith("55")) clean = `55${clean}`;
  // 55 + DDD (2) + número (8 ou 9) = 12 ou 13 dígitos
  if (clean.length < 12 || clean.length > 13) return null;
  return clean;
}

/**
 * Retorna JID WhatsApp (número@s.whatsapp.net) ou null se inválido.
 */
function formatPhoneForWhatsApp(phone) {
  const validated = validatePhoneNumber(phone);
  if (!validated) return null;
  return `${validated}@s.whatsapp.net`;
}

/**
 * Formata mensagem de evento de sistema (erros, startup, heartbeat).
 */
function formatSystemMessage(event) {
  const now = dayjs().tz("America/Sao_Paulo");
  const dataHora = now.format("DD/MM/YYYY HH:mm:ss");

  const levelEmojis = { info: "ℹ️", warn: "⚠️", error: "❌", fatal: "🚨" };
  const emoji = levelEmojis[event.level] || "📌";

  let msg = `${emoji} *${event.type?.toUpperCase()} | ${event.level?.toUpperCase()}*\n\n`;
  if (event.description) msg += `📝 ${event.description}\n\n`;
  if (event.message) msg += `💥 ${event.message}\n`;
  if (event.stack) msg += `\n📄 Stack:\n${event.stack.substring(0, 1200)}\n`;
  msg += `\n🕒 ${dataHora}`;
  return msg;
}

/**
 * Envia evento de sistema para o número de alerta configurado.
 * Usado para heartbeat, startup e erros internos.
 */
async function sendEvent(sock, event) {
  try {
    if (!sock || !event?.type) return false;

    if (!event.force) {
      const hash = hashEvent(event);
      const now = Date.now();
      if (hash === eventCache.lastHash && now - eventCache.lastSent < eventCache.ttl) return false;
      eventCache.lastHash = hash;
      eventCache.lastSent = now;
    }

    const alertPhone = process.env.WA_ALERT_PHONE;
    if (!alertPhone) return false;

    const jid = formatPhoneForWhatsApp(alertPhone);
    if (!jid) return false;

    const message = formatSystemMessage(event);
    await sock.sendMessage(jid, { text: message });
    return true;
  } catch (err) {
    console.error("[sendEvent] Erro:", err.message);
    return false;
  }
}

module.exports = { sendEvent, validatePhoneNumber, formatPhoneForWhatsApp };
