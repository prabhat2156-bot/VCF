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

"use strict";

const express     = require("express");
const { default: makeWASocket, useMultiFileAuthState, DisconnectReason,
        fetchLatestBaileysVersion, jidNormalizedUser } = require("@whiskeysockets/baileys");
const { Boom }    = require("@hapi/boom");
const pino        = require("pino");
const qrcode      = require("qrcode");
const fs          = require("fs");
const path        = require("path");

// ── Configuration ─────────────────────────────────────────────────
const PORT          = process.env.PORT || process.env.BRIDGE_PORT || 3000;
const SECRET        = process.env.BRIDGE_SECRET || "your_secret_key";
const SESSIONS_ROOT = path.join(__dirname, "wa_sessions");
const START_TIME    = Date.now();

if (!fs.existsSync(SESSIONS_ROOT)) fs.mkdirSync(SESSIONS_ROOT, { recursive: true });

// ── Logger ────────────────────────────────────────────────────────
const logger = pino({ level: "info" }, pino.destination("bridge.log"));

// ── In-memory socket map — all active sockets ─────────────────────
/** @type {Map<string, {sock: any, qrData: string, connected: boolean, name: string, phone: string, pairingCode: string}>} */
const sockets = new Map();

// ── Express App ───────────────────────────────────────────────────
const app = express();
app.use(express.json({ limit: "50mb" }));

// ── CORS Middleware ───────────────────────────────────────────────
// Allows cross-origin requests when bot and bridge run on separate Render services
app.use((req, res, next) => {
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type, x-secret, Authorization");
  if (req.method === "OPTIONS") {
    // Preflight — respond immediately
    return res.sendStatus(204);
  }
  next();
});

// ── Request Logging Middleware ────────────────────────────────────
app.use((req, res, next) => {
  const start = Date.now();
  res.on("finish", () => {
    const duration = Date.now() - start;
    console.log(`[${new Date().toISOString()}] ${req.method} ${req.path} → ${res.statusCode} (${duration}ms)`);
    logger.info({
      method: req.method,
      path: req.path,
      status: res.statusCode,
      durationMs: duration,
    }, "Request");
  });
  next();
});

// ── Auth Middleware / Security ────────────────────────────────────
// Applied after /health and /ping so those remain public
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

// ── /ping — Quick liveness check ─────────────────────────────────
app.get("/ping", (req, res) => {
  res.json({ pong: true, timestamp: new Date().toISOString() });
});

// ── /health — Full health report ─────────────────────────────────
app.get("/health", (req, res) => {
  const uptimeMs     = Date.now() - START_TIME;
  const uptimeSec    = Math.floor(uptimeMs / 1000);
  const uptimeMin    = Math.floor(uptimeSec / 60);
  const uptimeHr     = Math.floor(uptimeMin / 60);
  const memUsage     = process.memoryUsage();

  // Count connected vs total sessions
  let connectedCount = 0;
  sockets.forEach(entry => { if (entry.connected) connectedCount++; });

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
      rss:      `${Math.round(memUsage.rss / 1024 / 1024)} MB`,
      heapUsed: `${Math.round(memUsage.heapUsed / 1024 / 1024)} MB`,
      heapTotal:`${Math.round(memUsage.heapTotal / 1024 / 1024)} MB`,
      external: `${Math.round(memUsage.external / 1024 / 1024)} MB`,
    },
    node:    process.version,
    env:     process.env.NODE_ENV || "development",
    port:    PORT,
  });
});

// ── Apply secret-based auth to all remaining routes ───────────────
app.use(requireSecret);

// ═══════════════════════════════════════════════════════════════
//                   HELPER FUNCTIONS
// ═══════════════════════════════════════════════════════════════

/**
 * Get the socket entry for an account
 */
function getSocket(accountId) {
  const entry = sockets.get(accountId);
  if (!entry) return null;
  return entry.sock;
}

/**
 * Convert a phone number to JID format
 */
function toJid(phone) {
  const clean = phone.replace(/[^0-9]/g, "");
  return `${clean}@s.whatsapp.net`;
}

/**
 * Extract group invite code from a WhatsApp link
 */
function extractInviteCode(link) {
  const match = link.match(/chat\.whatsapp\.com\/([A-Za-z0-9]+)/);
  return match ? match[1] : link;
}

/**
 * Send an error response
 */
function errRes(res, message, code = 500) {
  return res.status(code).json({ success: false, error: message });
}

// ═══════════════════════════════════════════════════════════════
//           RECONNECT HELPER — used after initial pairing
// ═══════════════════════════════════════════════════════════════

/**
 * Create a persistent reconnecting WhatsApp socket for an already-paired account.
 * Called automatically after pairing and on startup session restore.
 *
 * @param {string} accountId
 * @param {string|null} phoneNumber - stored for reference; not used to re-pair
 */
async function createConnection(accountId, phoneNumber = null) {
  const sessionDir = path.join(SESSIONS_ROOT, accountId);
  if (!fs.existsSync(sessionDir)) {
    logger.warn({ accountId }, "createConnection: session directory not found — skipping");
    return null;
  }

  const { state, saveCreds } = await useMultiFileAuthState(sessionDir);
  const { version } = await fetchLatestBaileysVersion();

  const sock = makeWASocket({
    version,
    logger: pino({ level: "silent" }),
    printQRInTerminal: false,
    auth: state,
    browser: ["WhatsApp Group Manager", "Chrome", "3.0.0"],
    generateHighQualityLinkPreview: false,
    syncFullHistory: false,
    connectTimeoutMs: 60000,
    keepAliveIntervalMs: 30000,
  });

  // Upsert or overwrite the socket map entry
  sockets.set(accountId, {
    sock,
    qrData: "",
    connected: false,
    name: "",
    phone: phoneNumber || sockets.get(accountId)?.phone || "",
    pairingCode: "",
  });

  sock.ev.on("creds.update", saveCreds);

  // Keep event loop alive — handle any incoming message event
  sock.ev.on("messages.upsert", () => {});

  sock.ev.on("connection.update", async (update) => {
    const { connection, lastDisconnect, qr } = update;

    if (qr) {
      // Store QR data in case it appears (should not happen for paired accounts)
      const entry = sockets.get(accountId);
      if (entry) {
        entry.qrData = qr;
        sockets.set(accountId, entry);
      }
    }

    if (connection === "open") {
      const entry = sockets.get(accountId);
      if (entry) {
        entry.connected = true;
        entry.name  = sock.user?.name || accountId;
        entry.phone = sock.user?.id?.split(":")[0] || entry.phone || "";
        sockets.set(accountId, entry);
      }
      logger.info({ accountId }, "WhatsApp reconnected successfully");
    }

    if (connection === "close") {
      const statusCode = new Boom(lastDisconnect?.error)?.output?.statusCode;
      const shouldReconnect = statusCode !== DisconnectReason.loggedOut;

      const entry = sockets.get(accountId);
      if (entry) {
        entry.connected = false;
        sockets.set(accountId, entry);
      }

      if (shouldReconnect) {
        logger.info({ accountId, statusCode }, "Connection closed — reconnecting in 5s...");
        setTimeout(() => createConnection(accountId, phoneNumber), 5000);
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
// This endpoint:
//   1. Creates a fresh socket
//   2. Waits 5 s for Baileys to fully initialize (critical!)
//   3. Requests pairing code and returns it directly in the response
//
app.post("/connect/phone", async (req, res) => {
  const { accountId, phone } = req.body;
  if (!accountId || !phone) return errRes(res, "accountId and phone required", 400);

  try {
    const sessionDir = path.join(SESSIONS_ROOT, accountId);
    if (!fs.existsSync(sessionDir)) fs.mkdirSync(sessionDir, { recursive: true });

    const { state, saveCreds } = await useMultiFileAuthState(sessionDir);
    const { version } = await fetchLatestBaileysVersion();

    const sock = makeWASocket({
      version,
      logger: pino({ level: "silent" }),
      printQRInTerminal: false,
      auth: state,
      browser: ["WhatsApp Group Manager", "Chrome", "3.0.0"],
      generateHighQualityLinkPreview: false,
      syncFullHistory: false,
      connectTimeoutMs: 60000,
      keepAliveIntervalMs: 30000,
    });

    // Store socket immediately so event handlers can find it
    sockets.set(accountId, {
      sock,
      qrData: "",
      connected: false,
      name: "",
      phone: phone,
      pairingCode: "",
    });

    sock.ev.on("creds.update", saveCreds);

    // Keep event loop alive
    sock.ev.on("messages.upsert", () => {});

    sock.ev.on("connection.update", async (update) => {
      const { connection, lastDisconnect } = update;

      if (connection === "open") {
        const entry = sockets.get(accountId);
        if (entry) {
          entry.connected = true;
          entry.name  = sock.user?.name || accountId;
          entry.phone = sock.user?.id?.split(":")[0] || phone;
          sockets.set(accountId, entry);
        }
        logger.info({ accountId }, "WhatsApp connected via phone pairing");
      }

      if (connection === "close") {
        const statusCode = new Boom(lastDisconnect?.error)?.output?.statusCode;
        const shouldReconnect = statusCode !== DisconnectReason.loggedOut;

        const entry = sockets.get(accountId);
        if (entry) entry.connected = false;

        if (shouldReconnect) {
          logger.info({ accountId, statusCode }, "Reconnecting after pairing close in 5s...");
          setTimeout(() => createConnection(accountId, phone), 5000);
        } else {
          logger.info({ accountId }, "Logged out during pairing");
          sockets.delete(accountId);
        }
      }
    });

    // ── CRITICAL FIX: Wait for Baileys to fully initialize before requesting pairing code ──
    // The socket must finish its internal handshake before requestPairingCode() is called.
    // A 5-second delay is reliable across network conditions.
    logger.info({ accountId }, "Waiting 5s for socket initialization before pairing code request...");
    await new Promise(resolve => setTimeout(resolve, 5000));

    // Request pairing code
    const cleanPhone = phone.replace(/[^0-9]/g, "");
    logger.info({ accountId, cleanPhone }, "Requesting pairing code...");

    try {
      const code = await sock.requestPairingCode(cleanPhone);
      logger.info({ accountId, code }, "Pairing code received!");

      const entry = sockets.get(accountId);
      if (entry) {
        entry.pairingCode = code;
        sockets.set(accountId, entry);
      }

      // Return code directly — no polling needed
      return res.json({
        success: true,
        accountId,
        code: code,
        message: "Enter this code in WhatsApp → Linked Devices → Link a Device → Link with phone number",
      });
    } catch (pairErr) {
      logger.error({ accountId, err: pairErr.message }, "Pairing code request failed");
      return res.json({
        success: false,
        error: `Pairing code failed: ${pairErr.message}. Make sure the phone number is correct and not already linked.`,
      });
    }

  } catch (e) {
    logger.error({ accountId, err: e.message }, "Phone connect error");
    return errRes(res, e.message);
  }
});

// ── Connect via QR Code (FIXED) ───────────────────────────────────
// This endpoint:
//   1. Creates a fresh socket
//   2. Waits up to 30 s for the first QR code to arrive
//   3. Returns the raw QR data immediately — caller renders it
//   4. After scan, connection.update fires "open" and session is saved
//
app.post("/connect/qr", async (req, res) => {
  const { accountId } = req.body;
  if (!accountId) return errRes(res, "accountId required", 400);

  try {
    const sessionDir = path.join(SESSIONS_ROOT, accountId);
    if (!fs.existsSync(sessionDir)) fs.mkdirSync(sessionDir, { recursive: true });

    const { state, saveCreds } = await useMultiFileAuthState(sessionDir);
    const { version } = await fetchLatestBaileysVersion();

    // Promise resolves with the first QR string (or null on timeout / already connected)
    let qrResolve = null;
    const qrPromise = new Promise((resolve) => {
      qrResolve = resolve;
      setTimeout(() => resolve(null), 30000); // 30-second timeout
    });

    const sock = makeWASocket({
      version,
      logger: pino({ level: "silent" }),
      printQRInTerminal: true,  // Also log to terminal for debugging
      auth: state,
      browser: ["WhatsApp Group Manager", "Chrome", "3.0.0"],
      generateHighQualityLinkPreview: false,
      syncFullHistory: false,
      connectTimeoutMs: 60000,
      keepAliveIntervalMs: 30000,
    });

    sockets.set(accountId, {
      sock,
      qrData: "",
      connected: false,
      name: "",
      phone: "",
      pairingCode: "",
    });

    sock.ev.on("creds.update", saveCreds);

    // Keep event loop alive
    sock.ev.on("messages.upsert", () => {});

    sock.ev.on("connection.update", async (update) => {
      const { connection, lastDisconnect, qr } = update;

      if (qr) {
        logger.info({ accountId }, "QR code generated");
        const entry = sockets.get(accountId);
        if (entry) {
          entry.qrData = qr;
          sockets.set(accountId, entry);
        }

        // Resolve promise with QR data on first QR
        if (qrResolve) {
          qrResolve(qr);
          qrResolve = null; // Only resolve once
        }
      }

      if (connection === "open") {
        const entry = sockets.get(accountId);
        if (entry) {
          entry.connected = true;
          entry.name  = sock.user?.name || accountId;
          entry.phone = sock.user?.id?.split(":")[0] || "";
          sockets.set(accountId, entry);
        }
        logger.info({ accountId }, "WhatsApp connected via QR scan");

        // If QR promise is still pending (e.g. already-linked session opened immediately)
        if (qrResolve) {
          qrResolve(null);
          qrResolve = null;
        }
      }

      if (connection === "close") {
        const statusCode = new Boom(lastDisconnect?.error)?.output?.statusCode;
        const shouldReconnect = statusCode !== DisconnectReason.loggedOut;

        const entry = sockets.get(accountId);
        if (entry) entry.connected = false;

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

    // Await QR generation (up to 30 s)
    const qrData = await qrPromise;

    if (qrData) {
      return res.json({ success: true, accountId, qrData });
    }

    // No QR generated — check if session was already linked (no QR needed)
    const entry = sockets.get(accountId);
    if (entry && entry.connected) {
      return res.json({
        success: true,
        accountId,
        connected: true,
        message: "Session already active — no QR needed.",
      });
    }

    return res.json({
      success: false,
      error: "QR code generation timed out. Please try again.",
    });

  } catch (e) {
    logger.error({ accountId: req.body.accountId, err: e.message }, "QR connect error");
    return errRes(res, e.message);
  }
});

// ── Get Pairing Code (polling fallback) ──────────────────────────
// The /connect/phone endpoint now returns the code directly in the response.
// This endpoint exists only as a fallback for polling clients.
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
    success: true,
    connected: entry.connected,
    name: entry.name,
    phone: entry.phone,
    hasPairingCode: !!entry.pairingCode,
    hasQr: !!entry.qrData,
  });
});

// ── Disconnect / Logout ───────────────────────────────────────────
app.post("/disconnect", async (req, res) => {
  const { accountId } = req.body;
  if (!accountId) return errRes(res, "accountId required", 400);

  try {
    const sock = getSocket(accountId);
    if (sock) {
      await sock.logout();
    }
    sockets.delete(accountId);

    // Delete session files
    const sessionDir = path.join(SESSIONS_ROOT, accountId);
    if (fs.existsSync(sessionDir)) {
      fs.rmSync(sessionDir, { recursive: true, force: true });
    }

    res.json({ success: true, message: "Disconnected and session deleted" });
  } catch (e) {
    // Even on error, clean up the in-memory reference
    sockets.delete(accountId);
    res.json({ success: true, message: "Disconnected (with error)", error: e.message });
  }
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
      name: result.subject,
      message: `Group "${name}" created`,
    });
  } catch (e) {
    logger.error({ accountId, name, err: e.message }, "Create group error");
    errRes(res, e.message);
  }
});

// ── Set Group Photo ───────────────────────────────────────────────
app.post("/group/photo", async (req, res) => {
  const { accountId, groupId, photo } = req.body;
  if (!accountId || !groupId || !photo) return errRes(res, "Missing fields", 400);

  try {
    const sock = getSocket(accountId);
    if (!sock) return errRes(res, "Account not connected", 404);

    const buffer = Buffer.from(photo, "base64");
    await sock.updateProfilePicture(groupId, buffer);
    res.json({ success: true });
  } catch (e) {
    errRes(res, e.message);
  }
});

// ── Set Disappearing Messages ─────────────────────────────────────
app.post("/group/disappear", async (req, res) => {
  const { accountId, groupId, duration } = req.body;
  if (!accountId || !groupId) return errRes(res, "Missing fields", 400);

  try {
    const sock = getSocket(accountId);
    if (!sock) return errRes(res, "Account not connected", 404);

    // duration: 0=off, 86400=24h, 604800=7d, 7776000=90d
    await sock.groupToggleEphemeral(groupId, duration || 0);
    res.json({ success: true });
  } catch (e) {
    errRes(res, e.message);
  }
});

// ── Set Group Permissions ─────────────────────────────────────────
app.post("/group/permissions", async (req, res) => {
  const { accountId, groupId, permissions } = req.body;
  if (!accountId || !groupId || !permissions) return errRes(res, "Missing fields", 400);

  try {
    const sock = getSocket(accountId);
    if (!sock) return errRes(res, "Account not connected", 404);

    // Send messages permission
    if (permissions.send_messages !== undefined) {
      await sock.groupSettingUpdate(
        groupId,
        permissions.send_messages ? "not_announcement" : "announcement"
      );
    }

    // Edit group info permission
    if (permissions.edit_group_info !== undefined) {
      await sock.groupSettingUpdate(
        groupId,
        permissions.edit_group_info ? "unlocked" : "locked"
      );
    }

    // Add members permission
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

// ── Get All Groups ────────────────────────────────────────────────
app.get("/groups/:accountId", async (req, res) => {
  const { accountId } = req.params;

  try {
    const sock = getSocket(accountId);
    if (!sock) return errRes(res, "Account not connected", 404);

    const groups = await sock.groupFetchAllParticipating();
    const groupList = Object.values(groups).map(g => ({
      id: g.id,
      name: g.subject,
      description: g.desc || "",
      participantCount: g.participants?.length || 0,
      creation: g.creation,
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
      success: true,
      id: metadata.id,
      name: metadata.subject,
      description: metadata.desc || "",
      participantCount: metadata.participants?.length || 0,
      admins: metadata.participants?.filter(p => p.admin)?.map(p => p.id) || [],
    });
  } catch (e) {
    errRes(res, e.message);
  }
});

// ── Join Group via Invite Link ────────────────────────────────────
app.post("/group/join", async (req, res) => {
  const { accountId, link } = req.body;
  if (!accountId || !link) return errRes(res, "Missing fields", 400);

  try {
    const sock = getSocket(accountId);
    if (!sock) return errRes(res, "Account not connected", 404);

    const code = extractInviteCode(link);
    const result = await sock.groupAcceptInvite(code);
    res.json({ success: true, groupId: result, message: "Joined successfully" });
  } catch (e) {
    logger.error({ accountId, link, err: e.message }, "Join group error");
    errRes(res, e.message);
  }
});

// ── Get Invite Link ───────────────────────────────────────────────
app.get("/group/invite/:accountId/:groupId", async (req, res) => {
  const { accountId, groupId } = req.params;

  try {
    const sock = getSocket(accountId);
    if (!sock) return errRes(res, "Account not connected", 404);

    const code = await sock.groupInviteCode(groupId);
    res.json({
      success: true,
      link: `https://chat.whatsapp.com/${code}`,
      code,
    });
  } catch (e) {
    errRes(res, e.message);
  }
});

// ── Leave Group ───────────────────────────────────────────────────
app.post("/group/leave", async (req, res) => {
  const { accountId, groupId } = req.body;
  if (!accountId || !groupId) return errRes(res, "Missing fields", 400);

  try {
    const sock = getSocket(accountId);
    if (!sock) return errRes(res, "Account not connected", 404);

    await sock.groupLeave(groupId);
    res.json({ success: true, message: "Left group" });
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
    const members = (metadata.participants || []).map(p => ({
      jid: p.id,
      isAdmin: p.admin === "admin" || p.admin === "superadmin",
      isSuperAdmin: p.admin === "superadmin",
      isSelf: p.id.split("@")[0] === sock.user?.id?.split(":")[0],
    }));

    res.json({ success: true, members, count: members.length });
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

// ── Set Approval (member join request) Setting ────────────────────
app.post("/group/approval", async (req, res) => {
  const { accountId, groupId, enabled } = req.body;
  if (!accountId || !groupId || enabled === undefined) return errRes(res, "Missing fields", 400);

  try {
    const sock = getSocket(accountId);
    if (!sock) return errRes(res, "Account not connected", 404);

    await sock.groupMemberRequestUpdate(groupId, enabled ? "on" : "off");
    res.json({ success: true, enabled });
  } catch (e) {
    // Fallback: some Baileys versions use groupSettingUpdate
    try {
      const sock2 = getSocket(accountId);
      await sock2.groupSettingUpdate(groupId, enabled ? "member_approval" : "no_member_approval");
      res.json({ success: true, enabled });
    } catch (e2) {
      errRes(res, e2.message);
    }
  }
});

// ── Get Pending Join Requests ─────────────────────────────────────
app.get("/group/pending/:accountId/:groupId", async (req, res) => {
  const { accountId, groupId } = req.params;

  try {
    const sock = getSocket(accountId);
    if (!sock) return errRes(res, "Account not connected", 404);

    const result = await sock.groupRequestParticipantsList(groupId);
    const pending = (result || []).map(p => ({
      jid: p.jid,
      phone: p.jid.replace("@s.whatsapp.net", ""),
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

// ── Add Member to Group ───────────────────────────────────────────
app.post("/group/add-member", async (req, res) => {
  const { accountId, groupId, phone } = req.body;
  if (!accountId || !groupId || !phone) return errRes(res, "Missing fields", 400);

  try {
    const sock = getSocket(accountId);
    if (!sock) return errRes(res, "Account not connected", 404);

    const jid = toJid(phone);
    const result = await sock.groupParticipantsUpdate(groupId, [jid], "add");

    const status = result?.[0]?.status;
    if (status === "200" || status === 200) {
      res.json({ success: true, message: "Added" });
    } else if (status === "403") {
      res.json({ success: false, error: "Privacy settings block adding", status });
    } else if (status === "408") {
      res.json({ success: false, error: "Number not on WhatsApp", status });
    } else {
      res.json({ success: true, message: "Add attempted", status });
    }
  } catch (e) {
    errRes(res, e.message);
  }
});

// ── Check if Phone Number is on WhatsApp ─────────────────────────
app.post("/check-number", async (req, res) => {
  const { accountId, phone } = req.body;
  if (!accountId || !phone) return errRes(res, "Missing fields", 400);

  try {
    const sock = getSocket(accountId);
    if (!sock) return errRes(res, "Account not connected", 404);

    const jid = toJid(phone);
    const [result] = await sock.onWhatsApp(jid);
    res.json({ success: true, exists: result?.exists || false, jid: result?.jid || jid });
  } catch (e) {
    errRes(res, e.message);
  }
});

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
      jid = clean.length > 15 ? `${clean}@g.us` : `${clean}@s.whatsapp.net`;
    }

    await sock.sendMessage(jid, { text });
    res.json({ success: true, message: "Sent", to: jid });
  } catch (e) {
    logger.error({ accountId, to, err: e.message }, "Send message error");
    errRes(res, e.message);
  }
});

// ── List All Sessions ─────────────────────────────────────────────
app.get("/sessions", (req, res) => {
  const sessions = [];
  for (const [accountId, entry] of sockets.entries()) {
    sessions.push({
      sessionId:     accountId,
      connected:     entry.connected,
      name:          entry.name,
      phone:         entry.phone,
      status:        entry.connected ? "connected" : "disconnected",
      hasPairingCode: !!entry.pairingCode,
      hasQr:         !!entry.qrData,
    });
  }
  res.json({ success: true, sessions, count: sessions.length });
});

// ═══════════════════════════════════════════════════════════════
//                   SESSION RESTORATION
// ═══════════════════════════════════════════════════════════════

/**
 * On startup, restore all saved WhatsApp sessions from disk.
 * Uses createConnection() which auto-reconnects without requesting a pairing code.
 */
async function restoreSessions() {
  if (!fs.existsSync(SESSIONS_ROOT)) return;

  const accountDirs = fs.readdirSync(SESSIONS_ROOT);
  for (const accountId of accountDirs) {
    const sessionDir = path.join(SESSIONS_ROOT, accountId);
    if (!fs.statSync(sessionDir).isDirectory()) continue;

    const credsFile = path.join(sessionDir, "creds.json");
    if (!fs.existsSync(credsFile)) continue;

    logger.info({ accountId }, "Restoring saved session...");
    try {
      await createConnection(accountId);
      logger.info({ accountId }, "Session restore initiated");
    } catch (e) {
      logger.error({ accountId, err: e.message }, "Session restore failed");
    }

    // Stagger reconnects to avoid rate limiting
    await new Promise(r => setTimeout(r, 2000));
  }
}

// ═══════════════════════════════════════════════════════════════
//                       START SERVER
// ═══════════════════════════════════════════════════════════════

// Bind to 0.0.0.0 explicitly — required for Render and other cloud platforms
app.listen(PORT, "0.0.0.0", async () => {
  console.log(`
╔══════════════════════════════════════════════════════╗
║  WhatsApp Bridge Server - RUNNING                    ║
║  Port:    ${String(PORT).padEnd(43)}║
║  Host:    0.0.0.0 (all interfaces)                   ║
║  Secret:  ${SECRET.substring(0, 4)}...${" ".repeat(42)}║
╚══════════════════════════════════════════════════════╝
  `);
  logger.info({ port: PORT, host: "0.0.0.0" }, "Bridge server started");

  // Restore saved sessions from disk
  await restoreSessions();
  logger.info("Session restore complete");
});

// ── Graceful Shutdown ─────────────────────────────────────────────
// Do NOT call logout() — just end the WebSocket connection.
// This preserves the session credentials on disk so they can be restored on next start.

process.on("SIGTERM", () => {
  logger.info("SIGTERM received — shutting down gracefully...");
  for (const [accountId, entry] of sockets.entries()) {
    try { entry.sock.end(); } catch (_) {}
  }
  process.exit(0);
});

process.on("SIGINT", () => {
  logger.info("SIGINT received — shutting down gracefully...");
  for (const [accountId, entry] of sockets.entries()) {
    try { entry.sock.end(); } catch (_) {}
  }
  process.exit(0);
});
