// ╔══════════════════════════════════════════════════════════════════╗
// ║       WhatsApp Bridge Server - bridge.js                         ║
// ║       Node.js + @whiskeysockets/baileys + Express                ║
// ║       REST API Bridge for Python Telegram Bot                    ║
// ║       Render-compatible deployment                               ║
// ╚══════════════════════════════════════════════════════════════════╝

// SETUP:
//   npm install @whiskeysockets/baileys @hapi/boom pino express qrcode
//   node bridge.js

// ENVIRONMENT VARIABLES:
//   PORT          - HTTP port (set automatically by Render)
//   BRIDGE_PORT   - Fallback port override
//   BRIDGE_SECRET - Shared secret for API authentication (required in production)

// PACKAGE.JSON DEPENDENCIES (use these exact versions):
// {
//   "dependencies": {
//     "@whiskeysockets/baileys": "^6.7.9",
//     "@hapi/boom": "^10.0.1",
//     "pino": "^9.0.0",
//     "express": "^4.21.0",
//     "qrcode": "^1.5.4"
//   }
// }

"use strict";

const express = require("express");
const {
  default: makeWASocket,
  useMultiFileAuthState,
  DisconnectReason,
  fetchLatestBaileysVersion,
  makeCacheableSignalKeyStore,
  Browsers,
  jidNormalizedUser,
} = require("@whiskeysockets/baileys");
const { Boom } = require("@hapi/boom");
const pino     = require("pino");
const qrcode   = require("qrcode");
const fs       = require("fs");
const path     = require("path");

// ── Configuration ─────────────────────────────────────────────────
const PORT          = process.env.PORT || process.env.BRIDGE_PORT || 3000;
const SECRET        = process.env.BRIDGE_SECRET || "your_secret_key";
const SESSIONS_ROOT = path.join(__dirname, "wa_sessions");
const START_TIME    = Date.now();

if (!fs.existsSync(SESSIONS_ROOT)) fs.mkdirSync(SESSIONS_ROOT, { recursive: true });

// ── Logger ────────────────────────────────────────────────────────
// Write to both stdout and file for Render log visibility
const logger = pino(
  { level: "info" },
  pino.multistream([
    { stream: process.stdout },
    { stream: fs.createWriteStream(path.join(__dirname, "bridge.log"), { flags: "a" }) },
  ])
);

// ── In-memory socket map ───────────────────────────────────────────
/**
 * @type {Map<string, {
 *   sock: any,
 *   qrData: string,
 *   connected: boolean,
 *   name: string,
 *   phone: string,
 *   pairingCode: string
 * }>}
 */
const sockets = new Map();

// ── Express App ───────────────────────────────────────────────────
const app = express();
app.use(express.json({ limit: "50mb" }));

// ── CORS Middleware ───────────────────────────────────────────────
app.use((req, res, next) => {
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type, x-secret, Authorization");
  if (req.method === "OPTIONS") return res.sendStatus(204);
  next();
});

// ── Request Logging Middleware ────────────────────────────────────
app.use((req, res, next) => {
  const start = Date.now();
  res.on("finish", () => {
    const duration = Date.now() - start;
    console.log(
      `[${new Date().toISOString()}] ${req.method} ${req.path} → ${res.statusCode} (${duration}ms)`
    );
    logger.info(
      { method: req.method, path: req.path, status: res.statusCode, durationMs: duration },
      "Request"
    );
  });
  next();
});

// ── Auth Middleware ────────────────────────────────────────────────
function requireSecret(req, res, next) {
  const secret = req.headers["x-secret"];
  if (secret !== SECRET) {
    return res.status(401).json({ success: false, error: "Unauthorized" });
  }
  next();
}

// ═══════════════════════════════════════════════════════════════
//                PUBLIC HEALTH ENDPOINTS (no auth)
// ═══════════════════════════════════════════════════════════════

app.get("/ping", (req, res) => {
  res.json({ pong: true, timestamp: new Date().toISOString() });
});

app.get("/health", (req, res) => {
  const uptimeMs  = Date.now() - START_TIME;
  const uptimeSec = Math.floor(uptimeMs / 1000);
  const uptimeMin = Math.floor(uptimeSec / 60);
  const uptimeHr  = Math.floor(uptimeMin / 60);
  const mem       = process.memoryUsage();

  let connectedCount = 0;
  sockets.forEach((entry) => { if (entry.connected) connectedCount++; });

  res.json({
    success: true,
    status: "Bridge running",
    uptime: {
      ms:      uptimeMs,
      seconds: uptimeSec,
      minutes: uptimeMin,
      hours:   uptimeHr,
      human:   `${uptimeHr}h ${uptimeMin % 60}m ${uptimeSec % 60}s`,
    },
    sessions: {
      total:     sockets.size,
      connected: connectedCount,
    },
    memory: {
      rss:       `${Math.round(mem.rss / 1024 / 1024)} MB`,
      heapUsed:  `${Math.round(mem.heapUsed / 1024 / 1024)} MB`,
      heapTotal: `${Math.round(mem.heapTotal / 1024 / 1024)} MB`,
      external:  `${Math.round(mem.external / 1024 / 1024)} MB`,
    },
    node: process.version,
    env:  process.env.NODE_ENV || "development",
    port: PORT,
  });
});

// Apply auth to all routes below this line
app.use(requireSecret);

// ═══════════════════════════════════════════════════════════════
//                   HELPER FUNCTIONS
// ═══════════════════════════════════════════════════════════════

function getSocket(accountId) {
  const entry = sockets.get(accountId);
  return entry ? entry.sock : null;
}

function toJid(phone) {
  const clean = phone.replace(/[^0-9]/g, "");
  return `${clean}@s.whatsapp.net`;
}

function extractInviteCode(link) {
  const match = link.match(/chat\.whatsapp\.com\/([A-Za-z0-9]+)/);
  return match ? match[1] : link;
}

function errRes(res, message, code = 500) {
  return res.status(code).json({ success: false, error: message });
}

// ═══════════════════════════════════════════════════════════════
//      RECONNECT HELPER — restores sessions & reconnects after drops
// ═══════════════════════════════════════════════════════════════

/**
 * Create a persistent, auto-reconnecting WhatsApp socket for an
 * already-paired (registered) account.
 *
 * Uses:
 *   - makeCacheableSignalKeyStore  → fixes "couldn't link devices" signal errors
 *   - Browsers.ubuntu("Chrome")   → most-compatible browser fingerprint
 *
 * @param {string} accountId
 * @param {string|null} phoneNumber
 */
async function createConnection(accountId, phoneNumber = null) {
  const sessionDir = path.join(SESSIONS_ROOT, accountId);
  if (!fs.existsSync(sessionDir)) {
    logger.warn({ accountId }, "createConnection: session directory not found — skipping");
    return null;
  }

  const credsFile = path.join(sessionDir, "creds.json");
  if (!fs.existsSync(credsFile)) {
    logger.warn({ accountId }, "createConnection: creds.json not found — skipping");
    return null;
  }

  const { state, saveCreds } = await useMultiFileAuthState(sessionDir);
  const { version }          = await fetchLatestBaileysVersion();

  const sock = makeWASocket({
    version,
    logger: pino({ level: "silent" }),
    printQRInTerminal: false,
    auth: {
      creds: state.creds,
      keys:  makeCacheableSignalKeyStore(state.keys, pino({ level: "silent" })),
    },
    browser:                     Browsers.ubuntu("Chrome"),
    generateHighQualityLinkPreview: false,
    syncFullHistory:             false,
    connectTimeoutMs:            60000,
    keepAliveIntervalMs:         30000,
    markOnlineOnConnect:         false,
  });

  sockets.set(accountId, {
    sock,
    qrData:      "",
    connected:   false,
    name:        "",
    phone:       phoneNumber || sockets.get(accountId)?.phone || "",
    pairingCode: "",
  });

  sock.ev.on("creds.update", saveCreds);

  // Keep event loop alive
  sock.ev.on("messages.upsert", () => {});

  sock.ev.on("connection.update", (update) => {
    const { connection, lastDisconnect, qr } = update;

    if (qr) {
      const entry = sockets.get(accountId);
      if (entry) { entry.qrData = qr; sockets.set(accountId, entry); }
    }

    if (connection === "open") {
      const entry = sockets.get(accountId);
      if (entry) {
        entry.connected = true;
        entry.name      = sock.user?.name || accountId;
        entry.phone     = sock.user?.id?.split(":")[0] || entry.phone || "";
        sockets.set(accountId, entry);
      }
      logger.info({ accountId }, "WhatsApp reconnected successfully");
    }

    if (connection === "close") {
      const statusCode      = new Boom(lastDisconnect?.error)?.output?.statusCode;
      const shouldReconnect = statusCode !== DisconnectReason.loggedOut;

      const entry = sockets.get(accountId);
      if (entry) { entry.connected = false; sockets.set(accountId, entry); }

      if (shouldReconnect) {
        logger.info({ accountId, statusCode }, "Connection closed — reconnecting in 10s...");
        setTimeout(() => createConnection(accountId, phoneNumber), 10000);
      } else {
        logger.info({ accountId }, "Logged out — removing session");
        sockets.delete(accountId);
        const dir = path.join(SESSIONS_ROOT, accountId);
        if (fs.existsSync(dir)) fs.rmSync(dir, { recursive: true, force: true });
      }
    }
  });

  return sock;
}

// ═══════════════════════════════════════════════════════════════
//                        API ROUTES
// ═══════════════════════════════════════════════════════════════

// ── Connect via Phone Number (FIXED) ─────────────────────────────
//
// FIX SUMMARY:
//   1. Uses makeCacheableSignalKeyStore → prevents "couldn't link devices" signal errors
//   2. Uses Browsers.ubuntu("Chrome")  → stable browser fingerprint
//   3. Waits 3 s for socket to fully initialize BEFORE calling requestPairingCode()
//   4. Returns the pairing code directly in the HTTP response — no polling required
//
app.post("/connect/phone", async (req, res) => {
  const { accountId, phone } = req.body;
  if (!accountId || !phone) return errRes(res, "accountId and phone required", 400);

  // Disconnect any existing socket for this account
  const existing = sockets.get(accountId);
  if (existing) {
    try { existing.sock.end(undefined); } catch (_) {}
    sockets.delete(accountId);
  }

  try {
    const sessionDir = path.join(SESSIONS_ROOT, accountId);
    if (!fs.existsSync(sessionDir)) fs.mkdirSync(sessionDir, { recursive: true });

    const { state, saveCreds } = await useMultiFileAuthState(sessionDir);
    const { version }          = await fetchLatestBaileysVersion();

    const sock = makeWASocket({
      version,
      logger: pino({ level: "silent" }),
      printQRInTerminal: false,
      auth: {
        creds: state.creds,
        keys:  makeCacheableSignalKeyStore(state.keys, pino({ level: "silent" })),
      },
      browser:                     Browsers.ubuntu("Chrome"),
      generateHighQualityLinkPreview: false,
      syncFullHistory:             false,
      connectTimeoutMs:            60000,
      keepAliveIntervalMs:         30000,
      markOnlineOnConnect:         false,
    });

    const entry = {
      sock,
      qrData:      "",
      connected:   false,
      name:        "",
      phone:       phone,
      pairingCode: "",
    };
    sockets.set(accountId, entry);

    sock.ev.on("creds.update", saveCreds);
    sock.ev.on("messages.upsert", () => {});

    sock.ev.on("connection.update", (update) => {
      const { connection, lastDisconnect } = update;

      if (connection === "open") {
        entry.connected = true;
        entry.name      = sock.user?.name || accountId;
        entry.phone     = sock.user?.id?.split(":")[0] || phone;
        sockets.set(accountId, entry);
        logger.info({ accountId }, "Connected via phone pairing");
      }

      if (connection === "close") {
        entry.connected = false;
        const statusCode = new Boom(lastDisconnect?.error)?.output?.statusCode;
        if (statusCode !== DisconnectReason.loggedOut) {
          logger.info({ accountId, statusCode }, "Reconnecting after pairing close in 5s...");
          setTimeout(() => createConnection(accountId, phone), 5000);
        } else {
          logger.info({ accountId }, "Logged out during pairing");
          sockets.delete(accountId);
        }
      }
    });

    // CRITICAL: Do NOT request pairing code on an already-registered session
    if (state.creds.registered) {
      logger.info({ accountId }, "Session already registered, reconnecting without pairing");
      return res.json({
        success:   true,
        accountId,
        message:   "Session already registered — reconnecting automatically.",
        connected: entry.connected,
      });
    }

    // CRITICAL TIMING FIX:
    // Baileys must complete its WebSocket handshake and noise protocol setup
    // BEFORE requestPairingCode() is called. Without this wait the server
    // returns a 428 / "bad-request" error and WhatsApp never shows the notification.
    logger.info({ accountId }, "Waiting 3 s for socket initialization before pairing code...");
    await new Promise((resolve) => setTimeout(resolve, 3000));

    const cleanPhone = phone.replace(/[^0-9]/g, "");
    logger.info({ accountId, cleanPhone }, "Requesting pairing code...");

    try {
      const code = await sock.requestPairingCode(cleanPhone);
      entry.pairingCode = code;
      sockets.set(accountId, entry);
      logger.info({ accountId, code }, "Pairing code received");

      return res.json({
        success:  true,
        accountId,
        code,
        message:  "Enter this code in WhatsApp → Linked Devices → Link a Device → Link with phone number",
      });
    } catch (pairErr) {
      logger.error({ accountId, err: pairErr.message }, "Pairing code request failed");
      return res.json({
        success: false,
        error:   `Pairing code failed: ${pairErr.message}. ` +
                 "Ensure the phone number includes country code (e.g. 14155552671) and is not already linked.",
      });
    }
  } catch (e) {
    logger.error({ accountId, err: e.message }, "Phone connect error");
    return errRes(res, e.message);
  }
});

// ── Connect via QR Code (FIXED) ───────────────────────────────────
//
// FIX SUMMARY:
//   1. Uses makeCacheableSignalKeyStore + Browsers.ubuntu("Chrome")
//   2. Clears stale session files so a fresh QR is always generated
//   3. Promise-based: waits up to 45 s for QR, returns raw QR data
//   4. Caller renders the QR; after scan connection.update fires "open"
//
app.post("/connect/qr", async (req, res) => {
  const { accountId } = req.body;
  if (!accountId) return errRes(res, "accountId required", 400);

  // Disconnect existing socket if any
  const existing = sockets.get(accountId);
  if (existing) {
    if (existing.connected) {
      return res.json({
        success:   true,
        accountId,
        connected: true,
        message:   "Already connected — no QR needed.",
      });
    }
    try { existing.sock.end(undefined); } catch (_) {}
    sockets.delete(accountId);
  }

  try {
    const sessionDir = path.join(SESSIONS_ROOT, accountId);

    // Always start fresh: remove old creds so WhatsApp issues a new QR
    if (fs.existsSync(sessionDir)) {
      fs.rmSync(sessionDir, { recursive: true, force: true });
    }
    fs.mkdirSync(sessionDir, { recursive: true });

    const { state, saveCreds } = await useMultiFileAuthState(sessionDir);
    const { version }          = await fetchLatestBaileysVersion();

    let qrResolve = null;
    const qrPromise = new Promise((resolve) => {
      qrResolve = resolve;
      setTimeout(() => resolve(null), 45000); // 45-second timeout
    });

    const sock = makeWASocket({
      version,
      logger: pino({ level: "silent" }),
      printQRInTerminal: true,
      auth: {
        creds: state.creds,
        keys:  makeCacheableSignalKeyStore(state.keys, pino({ level: "silent" })),
      },
      browser:                     Browsers.ubuntu("Chrome"),
      generateHighQualityLinkPreview: false,
      syncFullHistory:             false,
      connectTimeoutMs:            60000,
      keepAliveIntervalMs:         30000,
      markOnlineOnConnect:         false,
    });

    const entry = {
      sock,
      qrData:      "",
      connected:   false,
      name:        "",
      phone:       "",
      pairingCode: "",
    };
    sockets.set(accountId, entry);

    sock.ev.on("creds.update", saveCreds);
    sock.ev.on("messages.upsert", () => {});

    sock.ev.on("connection.update", (update) => {
      const { connection, lastDisconnect, qr } = update;

      if (qr) {
        entry.qrData = qr;
        sockets.set(accountId, entry);
        logger.info({ accountId }, "QR code generated");
        if (qrResolve) {
          qrResolve(qr);
          qrResolve = null;
        }
      }

      if (connection === "open") {
        entry.connected = true;
        entry.name      = sock.user?.name || accountId;
        entry.phone     = sock.user?.id?.split(":")[0] || "";
        sockets.set(accountId, entry);
        logger.info({ accountId }, "Connected via QR scan");
        // Resolve in case still waiting (session already active edge case)
        if (qrResolve) { qrResolve(null); qrResolve = null; }
      }

      if (connection === "close") {
        entry.connected = false;
        const statusCode      = new Boom(lastDisconnect?.error)?.output?.statusCode;
        const shouldReconnect = statusCode !== DisconnectReason.loggedOut;
        if (shouldReconnect) {
          logger.info({ accountId, statusCode }, "Connection closed after QR — reconnecting in 5s...");
          setTimeout(() => createConnection(accountId), 5000);
        } else {
          logger.info({ accountId }, "Logged out — removing session");
          sockets.delete(accountId);
          const dir = path.join(SESSIONS_ROOT, accountId);
          if (fs.existsSync(dir)) fs.rmSync(dir, { recursive: true, force: true });
        }
      }
    });

    const qrData = await qrPromise;

    if (qrData) {
      return res.json({ success: true, accountId, qrData });
    }

    // Timed out — check if already connected (edge case: pre-linked session)
    const current = sockets.get(accountId);
    if (current && current.connected) {
      return res.json({
        success:   true,
        accountId,
        connected: true,
        message:   "Session already active — no QR needed.",
      });
    }

    return res.json({
      success: false,
      error:   "QR code generation timed out. Please try again.",
    });
  } catch (e) {
    logger.error({ accountId: req.body.accountId, err: e.message }, "QR connect error");
    return errRes(res, e.message);
  }
});

// ── Get Pairing Code (polling fallback) ──────────────────────────
// /connect/phone now returns the code directly in the response.
// This endpoint is kept for polling clients that need it.
app.get("/connect/pairing-code/:accountId", (req, res) => {
  const { accountId } = req.params;
  const entry = sockets.get(accountId);
  if (!entry) return errRes(res, "Account not found", 404);
  if (!entry.pairingCode) {
    return res.json({ success: false, code: null, message: "Pairing code not yet generated." });
  }
  res.json({ success: true, code: entry.pairingCode });
});

// ── Get Connection Status ─────────────────────────────────────────
app.get("/status/:accountId", (req, res) => {
  const { accountId } = req.params;
  const entry = sockets.get(accountId);
  if (!entry) return res.json({ success: true, connected: false });
  res.json({
    success:        true,
    connected:      entry.connected,
    name:           entry.name,
    phone:          entry.phone,
    hasPairingCode: !!entry.pairingCode,
    hasQr:          !!entry.qrData,
  });
});

// ── Disconnect / Logout ───────────────────────────────────────────
app.post("/disconnect", async (req, res) => {
  const { accountId } = req.body;
  if (!accountId) return errRes(res, "accountId required", 400);

  try {
    const sock = getSocket(accountId);
    if (sock) {
      try { await sock.logout(); } catch (_) {}
    }
    sockets.delete(accountId);

    const sessionDir = path.join(SESSIONS_ROOT, accountId);
    if (fs.existsSync(sessionDir)) {
      fs.rmSync(sessionDir, { recursive: true, force: true });
    }

    res.json({ success: true, message: "Disconnected and session deleted" });
  } catch (e) {
    sockets.delete(accountId);
    res.json({ success: true, message: "Disconnected (with error)", error: e.message });
  }
});

// ── List All Sessions ─────────────────────────────────────────────
app.get("/sessions", (req, res) => {
  const sessions = [];
  for (const [accountId, entry] of sockets.entries()) {
    sessions.push({
      sessionId:      accountId,
      connected:      entry.connected,
      name:           entry.name,
      phone:          entry.phone,
      status:         entry.connected ? "connected" : "disconnected",
      hasPairingCode: !!entry.pairingCode,
      hasQr:          !!entry.qrData,
    });
  }
  res.json({ success: true, sessions, count: sessions.length });
});

// ═══════════════════════════════════════════════════════════════
//                   GROUP MANAGEMENT ENDPOINTS
// ═══════════════════════════════════════════════════════════════

// ── Create Group ──────────────────────────────────────────────────
app.post("/group/create", async (req, res) => {
  const { accountId, name, members = [] } = req.body;
  if (!accountId || !name) return errRes(res, "accountId and name required", 400);

  try {
    const sock = getSocket(accountId);
    if (!sock) return errRes(res, "Account not connected", 404);

    const participantJids = members.map(toJid);
    const result = await sock.groupCreate(name, participantJids);

    res.json({
      success: true,
      groupId: result.id,
      name:    result.subject,
      message: `Group "${name}" created`,
    });
  } catch (e) {
    logger.error({ accountId, name, err: e.message }, "Create group error");
    errRes(res, e.message);
  }
});

// ── Set Group Profile Photo ───────────────────────────────────────
app.post("/group/photo", async (req, res) => {
  const { accountId, groupId, photo } = req.body;
  if (!accountId || !groupId || !photo) return errRes(res, "accountId, groupId, and photo (base64) required", 400);

  try {
    const sock = getSocket(accountId);
    if (!sock) return errRes(res, "Account not connected", 404);

    const buffer = Buffer.from(photo, "base64");
    await sock.updateProfilePicture(groupId, buffer);
    res.json({ success: true, message: "Group photo updated" });
  } catch (e) {
    errRes(res, e.message);
  }
});

// ── Set Disappearing Messages ─────────────────────────────────────
app.post("/group/disappear", async (req, res) => {
  const { accountId, groupId, duration } = req.body;
  if (!accountId || !groupId) return errRes(res, "accountId and groupId required", 400);

  try {
    const sock = getSocket(accountId);
    if (!sock) return errRes(res, "Account not connected", 404);

    // duration: 0 = off | 86400 = 24h | 604800 = 7d | 7776000 = 90d
    await sock.groupToggleEphemeral(groupId, duration || 0);
    res.json({ success: true, duration: duration || 0 });
  } catch (e) {
    errRes(res, e.message);
  }
});

// ── Set Group Permissions ─────────────────────────────────────────
app.post("/group/permissions", async (req, res) => {
  const { accountId, groupId, permissions } = req.body;
  if (!accountId || !groupId || !permissions) return errRes(res, "accountId, groupId, and permissions required", 400);

  try {
    const sock = getSocket(accountId);
    if (!sock) return errRes(res, "Account not connected", 404);

    // Restrict who can send messages
    if (permissions.send_messages !== undefined) {
      await sock.groupSettingUpdate(
        groupId,
        permissions.send_messages ? "not_announcement" : "announcement"
      );
    }

    // Restrict who can edit group info
    if (permissions.edit_group_info !== undefined) {
      await sock.groupSettingUpdate(
        groupId,
        permissions.edit_group_info ? "unlocked" : "locked"
      );
    }

    // Restrict who can add new members
    if (permissions.add_members !== undefined) {
      await sock.groupSettingUpdate(
        groupId,
        permissions.add_members ? "member_add_mode" : "admin_add_mode"
      );
    }

    res.json({ success: true });
  } catch (e) {
    errRes(res, e.message);
  }
});

// ── Set Membership Approval ───────────────────────────────────────
app.post("/group/approval", async (req, res) => {
  const { accountId, groupId, enabled } = req.body;
  if (!accountId || !groupId || enabled === undefined) return errRes(res, "Missing fields", 400);

  try {
    const sock = getSocket(accountId);
    if (!sock) return errRes(res, "Account not connected", 404);

    try {
      await sock.groupMemberRequestUpdate(groupId, enabled ? "on" : "off");
    } catch (_) {
      // Fallback for some Baileys builds
      await sock.groupSettingUpdate(
        groupId,
        enabled ? "member_approval" : "no_member_approval"
      );
    }

    res.json({ success: true, enabled });
  } catch (e) {
    errRes(res, e.message);
  }
});

// ── Get All Groups ────────────────────────────────────────────────
app.get("/groups/:accountId", async (req, res) => {
  const { accountId } = req.params;

  try {
    const sock = getSocket(accountId);
    if (!sock) return errRes(res, "Account not connected", 404);

    const groups    = await sock.groupFetchAllParticipating();
    const groupList = Object.values(groups).map((g) => ({
      id:               g.id,
      name:             g.subject,
      description:      g.desc || "",
      participantCount: g.participants?.length || 0,
      creation:         g.creation,
    }));

    res.json({ success: true, groups: groupList, count: groupList.length });
  } catch (e) {
    logger.error({ accountId, err: e.message }, "Get groups error");
    errRes(res, e.message);
  }
});

// ── Get Group Info ────────────────────────────────────────────────
app.get("/group/:accountId/:groupId", async (req, res) => {
  const { accountId, groupId } = req.params;

  try {
    const sock = getSocket(accountId);
    if (!sock) return errRes(res, "Account not connected", 404);

    const metadata = await sock.groupMetadata(groupId);
    res.json({
      success:          true,
      id:               metadata.id,
      name:             metadata.subject,
      description:      metadata.desc || "",
      participantCount: metadata.participants?.length || 0,
      admins:           metadata.participants?.filter((p) => p.admin)?.map((p) => p.id) || [],
    });
  } catch (e) {
    errRes(res, e.message);
  }
});

// ── Get Group Members ─────────────────────────────────────────────
app.get("/group/members/:accountId/:groupId", async (req, res) => {
  const { accountId, groupId } = req.params;

  try {
    const sock = getSocket(accountId);
    if (!sock) return errRes(res, "Account not connected", 404);

    const metadata = await sock.groupMetadata(groupId);
    const members  = (metadata.participants || []).map((p) => ({
      jid:          p.id,
      isAdmin:      p.admin === "admin" || p.admin === "superadmin",
      isSuperAdmin: p.admin === "superadmin",
      isSelf:       p.id.split("@")[0] === sock.user?.id?.split(":")[0],
    }));

    res.json({ success: true, members, count: members.length });
  } catch (e) {
    errRes(res, e.message);
  }
});

// ── Add Member to Group ───────────────────────────────────────────
app.post("/group/add-member", async (req, res) => {
  const { accountId, groupId, phone } = req.body;
  if (!accountId || !groupId || !phone) return errRes(res, "Missing fields", 400);

  try {
    const sock = getSocket(accountId);
    if (!sock) return errRes(res, "Account not connected", 404);

    const jid    = toJid(phone);
    const result = await sock.groupParticipantsUpdate(groupId, [jid], "add");
    const status = result?.[0]?.status;

    if (status === "200" || status === 200) {
      res.json({ success: true, message: "Added" });
    } else if (status === "403") {
      res.json({ success: false, error: "User privacy settings block adding", status });
    } else if (status === "408") {
      res.json({ success: false, error: "Number not on WhatsApp", status });
    } else {
      res.json({ success: true, message: "Add attempted", status });
    }
  } catch (e) {
    errRes(res, e.message);
  }
});

// ── Remove Member from Group ──────────────────────────────────────
app.post("/group/remove-member", async (req, res) => {
  const { accountId, groupId, memberJid } = req.body;
  if (!accountId || !groupId || !memberJid) return errRes(res, "Missing fields", 400);

  try {
    const sock = getSocket(accountId);
    if (!sock) return errRes(res, "Account not connected", 404);

    await sock.groupParticipantsUpdate(groupId, [memberJid], "remove");
    res.json({ success: true, message: "Member removed" });
  } catch (e) {
    errRes(res, e.message);
  }
});

// ── Promote Member to Admin ───────────────────────────────────────
app.post("/group/make-admin", async (req, res) => {
  const { accountId, groupId, memberJid } = req.body;
  if (!accountId || !groupId || !memberJid) return errRes(res, "Missing fields", 400);

  try {
    const sock = getSocket(accountId);
    if (!sock) return errRes(res, "Account not connected", 404);

    await sock.groupParticipantsUpdate(groupId, [memberJid], "promote");
    res.json({ success: true, message: "Promoted to admin" });
  } catch (e) {
    errRes(res, e.message);
  }
});

// ── Demote Admin to Member ────────────────────────────────────────
app.post("/group/remove-admin", async (req, res) => {
  const { accountId, groupId, memberJid } = req.body;
  if (!accountId || !groupId || !memberJid) return errRes(res, "Missing fields", 400);

  try {
    const sock = getSocket(accountId);
    if (!sock) return errRes(res, "Account not connected", 404);

    await sock.groupParticipantsUpdate(groupId, [memberJid], "demote");
    res.json({ success: true, message: "Removed as admin" });
  } catch (e) {
    errRes(res, e.message);
  }
});

// ── Join Group via Invite Link ────────────────────────────────────
app.post("/group/join", async (req, res) => {
  const { accountId, link } = req.body;
  if (!accountId || !link) return errRes(res, "accountId and link required", 400);

  try {
    const sock = getSocket(accountId);
    if (!sock) return errRes(res, "Account not connected", 404);

    const code   = extractInviteCode(link);
    const result = await sock.groupAcceptInvite(code);
    res.json({ success: true, groupId: result, message: "Joined successfully" });
  } catch (e) {
    logger.error({ accountId, link, err: e.message }, "Join group error");
    errRes(res, e.message);
  }
});

// ── Leave Group ───────────────────────────────────────────────────
app.post("/group/leave", async (req, res) => {
  const { accountId, groupId } = req.body;
  if (!accountId || !groupId) return errRes(res, "accountId and groupId required", 400);

  try {
    const sock = getSocket(accountId);
    if (!sock) return errRes(res, "Account not connected", 404);

    await sock.groupLeave(groupId);
    res.json({ success: true, message: "Left group" });
  } catch (e) {
    errRes(res, e.message);
  }
});

// ── Get Group Invite Link ─────────────────────────────────────────
app.get("/group/invite/:accountId/:groupId", async (req, res) => {
  const { accountId, groupId } = req.params;

  try {
    const sock = getSocket(accountId);
    if (!sock) return errRes(res, "Account not connected", 404);

    const code = await sock.groupInviteCode(groupId);
    res.json({
      success: true,
      link:    `https://chat.whatsapp.com/${code}`,
      code,
    });
  } catch (e) {
    errRes(res, e.message);
  }
});

// ── Get Pending Join Requests ─────────────────────────────────────
app.get("/group/pending/:accountId/:groupId", async (req, res) => {
  const { accountId, groupId } = req.params;

  try {
    const sock = getSocket(accountId);
    if (!sock) return errRes(res, "Account not connected", 404);

    const result  = await sock.groupRequestParticipantsList(groupId);
    const pending = (result || []).map((p) => ({
      jid:         p.jid,
      phone:       p.jid.replace("@s.whatsapp.net", ""),
      requestedAt: p.request_method,
    }));

    res.json({ success: true, pending, count: pending.length });
  } catch (e) {
    errRes(res, e.message);
  }
});

// ── Reject Pending Join Request ───────────────────────────────────
app.post("/group/reject-pending", async (req, res) => {
  const { accountId, groupId, memberJid } = req.body;
  if (!accountId || !groupId || !memberJid) return errRes(res, "Missing fields", 400);

  try {
    const sock = getSocket(accountId);
    if (!sock) return errRes(res, "Account not connected", 404);

    await sock.groupRequestParticipantsUpdate(groupId, [memberJid], "reject");
    res.json({ success: true, message: "Rejected" });
  } catch (e) {
    errRes(res, e.message);
  }
});

// ═══════════════════════════════════════════════════════════════
//                   MESSAGE ENDPOINTS
// ═══════════════════════════════════════════════════════════════

// ── Send Text Message ─────────────────────────────────────────────
app.post("/message/send", async (req, res) => {
  const { accountId, to, text } = req.body;
  if (!accountId || !to || !text) return errRes(res, "accountId, to, and text required", 400);

  try {
    const sock = getSocket(accountId);
    if (!sock) return errRes(res, "Account not connected", 404);

    let jid = to;
    if (!to.includes("@")) {
      const clean = to.replace(/[^0-9]/g, "");
      // Heuristic: group IDs are longer than 15 digits
      jid = clean.length > 15 ? `${clean}@g.us` : `${clean}@s.whatsapp.net`;
    }

    await sock.sendMessage(jid, { text });
    res.json({ success: true, message: "Sent", to: jid });
  } catch (e) {
    logger.error({ accountId, to, err: e.message }, "Send message error");
    errRes(res, e.message);
  }
});

// ═══════════════════════════════════════════════════════════════
//                   UTILITY ENDPOINTS
// ═══════════════════════════════════════════════════════════════

// ── Check if Phone Number is on WhatsApp ─────────────────────────
app.post("/check-number", async (req, res) => {
  const { accountId, phone } = req.body;
  if (!accountId || !phone) return errRes(res, "accountId and phone required", 400);

  try {
    const sock = getSocket(accountId);
    if (!sock) return errRes(res, "Account not connected", 404);

    const jid      = toJid(phone);
    const [result] = await sock.onWhatsApp(jid);
    res.json({ success: true, exists: result?.exists || false, jid: result?.jid || jid });
  } catch (e) {
    errRes(res, e.message);
  }
});

// ═══════════════════════════════════════════════════════════════
//                   SESSION RESTORATION
// ═══════════════════════════════════════════════════════════════

/**
 * On startup, restore all saved WhatsApp sessions from disk.
 * Uses createConnection() which never requests a pairing code.
 */
async function restoreSessions() {
  if (!fs.existsSync(SESSIONS_ROOT)) return;

  const accountDirs = fs.readdirSync(SESSIONS_ROOT);
  let restored = 0;

  for (const accountId of accountDirs) {
    const sessionDir = path.join(SESSIONS_ROOT, accountId);
    if (!fs.statSync(sessionDir).isDirectory()) continue;

    const credsFile = path.join(sessionDir, "creds.json");
    if (!fs.existsSync(credsFile)) continue;

    logger.info({ accountId }, "Restoring saved session...");
    try {
      await createConnection(accountId);
      restored++;
      logger.info({ accountId }, "Session restore initiated");
    } catch (e) {
      logger.error({ accountId, err: e.message }, "Session restore failed");
    }

    // Stagger reconnects to avoid WhatsApp rate limiting
    await new Promise((r) => setTimeout(r, 2000));
  }

  logger.info({ restored, total: accountDirs.length }, "Session restoration complete");
}

// ═══════════════════════════════════════════════════════════════
//                       START SERVER
// ═══════════════════════════════════════════════════════════════

// Bind to 0.0.0.0 explicitly — required for Render and other PaaS platforms
app.listen(PORT, "0.0.0.0", async () => {
  console.log(`
╔══════════════════════════════════════════════════════╗
║  WhatsApp Bridge Server - RUNNING                    ║
║  Port:    ${String(PORT).padEnd(43)}║
║  Host:    0.0.0.0 (all interfaces)                   ║
╚══════════════════════════════════════════════════════╝
  `);
  logger.info({ port: PORT, host: "0.0.0.0" }, "Bridge server started");
  await restoreSessions();
});

// ── Graceful Shutdown ─────────────────────────────────────────────
// Call sock.end() (not logout()) to preserve session credentials on disk.
// Sessions are restored automatically on next startup.

function gracefulShutdown(signal) {
  logger.info({ signal }, "Shutting down gracefully...");
  for (const [accountId, entry] of sockets.entries()) {
    try { entry.sock.end(undefined); } catch (_) {}
  }
  process.exit(0);
}

process.on("SIGTERM", () => gracefulShutdown("SIGTERM"));
process.on("SIGINT",  () => gracefulShutdown("SIGINT"));
