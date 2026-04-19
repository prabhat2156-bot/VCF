// ╔══════════════════════════════════════════════════════════════════╗
// ║       WhatsApp Bridge Server - bridge.js                         ║
// ║       Node.js + @whiskeysockets/baileys + Express                ║
// ║       REST API Bridge for Python Telegram Bot                    ║
// ╚══════════════════════════════════════════════════════════════════╝

// SETUP:
//   npm install @whiskeysockets/baileys @hapi/boom pino express qrcode
//   node bridge.js

"use strict";

const express     = require("express");
const { default: makeWASocket, useMultiFileAuthState, DisconnectReason,
        fetchLatestBaileysVersion, makeInMemoryStore, jidNormalizedUser,
        getAggregateVotesInPollMessage } = require("@whiskeysockets/baileys");
const { Boom }    = require("@hapi/boom");
const pino        = require("pino");
const qrcode      = require("qrcode");
const fs          = require("fs");
const path        = require("path");

// ── Configuration ─────────────────────────────────────────────────
const PORT          = process.env.BRIDGE_PORT   || 3000;
const SECRET        = process.env.BRIDGE_SECRET || "your_secret_key";
const SESSIONS_ROOT = path.join(__dirname, "wa_sessions");

if (!fs.existsSync(SESSIONS_ROOT)) fs.mkdirSync(SESSIONS_ROOT, { recursive: true });

// ── Logger ────────────────────────────────────────────────────────
const logger = pino({ level: "info" }, pino.destination("bridge.log"));

// ── In-memory socket map — all active sockets ─────────────────────
/** @type {Map<string, {sock: any, qrData: string, connected: boolean, name: string, phone: string}>} */
const sockets = new Map();

// ── Express App ───────────────────────────────────────────────────
const app = express();
app.use(express.json({ limit: "50mb" }));

// ── Auth Middleware / Security ────────────────────────────────────
app.use((req, res, next) => {
  const secret = req.headers["x-secret"];
  if (secret !== SECRET) {
    return res.status(401).json({ success: false, error: "Unauthorized" });
  }
  next();
});

// ═══════════════════════════════════════════════════════════════
//                   BAILEYS CONNECTION
// ═══════════════════════════════════════════════════════════════

/**
 * Create a WhatsApp connection
 * @param {string} accountId - Unique account identifier
 * @param {boolean} usePairingCode - Use phone number pairing
 * @param {string} phoneNumber - Phone number for pairing
 */
async function createConnection(accountId, usePairingCode = false, phoneNumber = null) {
  const sessionDir = path.join(SESSIONS_ROOT, accountId);
  if (!fs.existsSync(sessionDir)) fs.mkdirSync(sessionDir, { recursive: true });

  const { state, saveCreds } = await useMultiFileAuthState(sessionDir);
  const { version } = await fetchLatestBaileysVersion();

  const sock = makeWASocket({
    version,
    logger: pino({ level: "silent" }),
    printQRInTerminal: !usePairingCode,
    auth: state,
    browser: ["WhatsApp Group Manager", "Chrome", "3.0.0"],
    generateHighQualityLinkPreview: false,
    syncFullHistory: false,
  });

  // Store in socket map
  sockets.set(accountId, {
    sock,
    qrData: "",
    connected: false,
    name: "",
    phone: phoneNumber || "",
    pairingCode: "",
  });

  // ── QR Code Event ──────────────────────────────────────────
  sock.ev.on("connection.update", async (update) => {
    const { connection, lastDisconnect, qr } = update;

    if (qr) {
      // Store QR data
      const entry = sockets.get(accountId);
      if (entry) {
        entry.qrData = qr;
        sockets.set(accountId, entry);
      }
      logger.info({ accountId }, "QR code generated");
    }

    if (usePairingCode && !sock.authState.creds.registered && phoneNumber) {
      // Request pairing code
      try {
        const code = await sock.requestPairingCode(phoneNumber.replace(/[^0-9]/g, ""));
        const entry = sockets.get(accountId);
        if (entry) {
          entry.pairingCode = code;
          sockets.set(accountId, entry);
        }
        logger.info({ accountId, code }, "Pairing code generated");
      } catch (e) {
        logger.error({ accountId, err: e.message }, "Pairing code error");
      }
    }

    if (connection === "close") {
      const shouldReconnect =
        new Boom(lastDisconnect?.error)?.output?.statusCode !== DisconnectReason.loggedOut;

      logger.info({ accountId, shouldReconnect }, "Connection closed");

      const entry = sockets.get(accountId);
      if (entry) {
        entry.connected = false;
        sockets.set(accountId, entry);
      }

      // Auto-reconnect (if not logged out)
      if (shouldReconnect) {
        setTimeout(() => createConnection(accountId, usePairingCode, phoneNumber), 5000);
      } else {
        // Logged out — clean up session
        sockets.delete(accountId);
      }
    }

    if (connection === "open") {
      const entry = sockets.get(accountId);
      if (entry) {
        entry.connected = true;
        entry.name = sock.user?.name || accountId;
        entry.phone = sock.user?.id?.split(":")[0] || phoneNumber || "";
        sockets.set(accountId, entry);
      }
      logger.info({ accountId }, "WhatsApp connected successfully");
    }
  });

  // Save credentials
  sock.ev.on("creds.update", saveCreds);

  return sock;
}

// ═══════════════════════════════════════════════════════════════
//                     HELPER FUNCTIONS
// ═══════════════════════════════════════════════════════════════

/**
 * Get the socket for an account
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
 * Extract group ID from an invite link
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
//                        API ROUTES
// ═══════════════════════════════════════════════════════════════

// ── Health Check ──────────────────────────────────────────────
app.get("/health", (req, res) => {
  res.json({ success: true, status: "Bridge running", accounts: sockets.size });
});

// ── Connect via QR ────────────────────────────────────────────
app.post("/connect/qr", async (req, res) => {
  const { accountId } = req.body;
  if (!accountId) return errRes(res, "accountId required", 400);

  try {
    await createConnection(accountId, false, null);
    // Wait for QR to be generated (max 10 seconds)
    let attempts = 0;
    while (attempts < 20) {
      await new Promise(r => setTimeout(r, 500));
      const entry = sockets.get(accountId);
      if (entry && entry.qrData) {
        return res.json({ success: true, accountId, qrData: entry.qrData });
      }
      attempts++;
    }
    res.json({ success: true, accountId, qrData: "", message: "QR is being generated..." });
  } catch (e) {
    logger.error(e);
    errRes(res, e.message);
  }
});

// ── Connect via Phone Number ──────────────────────────────────
app.post("/connect/phone", async (req, res) => {
  const { accountId, phone } = req.body;
  if (!accountId || !phone) return errRes(res, "accountId and phone required", 400);

  try {
    await createConnection(accountId, true, phone);
    res.json({ success: true, accountId, message: "Connection initiated, pairing code incoming..." });
  } catch (e) {
    logger.error(e);
    errRes(res, e.message);
  }
});

// ── Get Pairing Code ──────────────────────────────────────────
app.get("/connect/pairing-code/:accountId", (req, res) => {
  const { accountId } = req.params;
  const entry = sockets.get(accountId);
  if (!entry) return errRes(res, "Account not found", 404);
  res.json({ success: true, code: entry.pairingCode || "Generating..." });
});

// ── Get Connection Status ─────────────────────────────────────
app.get("/status/:accountId", (req, res) => {
  const { accountId } = req.params;
  const entry = sockets.get(accountId);
  if (!entry) return res.json({ success: true, connected: false });
  res.json({
    success: true,
    connected: entry.connected,
    name: entry.name,
    phone: entry.phone,
  });
});

// ── Disconnect ────────────────────────────────────────────────
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

    res.json({ success: true, message: "Disconnected" });
  } catch (e) {
    // Even if error, remove from map
    sockets.delete(accountId);
    res.json({ success: true, message: "Disconnected (with error)" });
  }
});

// ── Create Group ──────────────────────────────────────────────
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

// ── Set Group Photo ───────────────────────────────────────────
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

// ── Set Disappearing Messages ─────────────────────────────────
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

// ── Set Group Permissions ─────────────────────────────────────
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

// ── Get All Groups ────────────────────────────────────────────
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

// ── Get Group Info ────────────────────────────────────────────
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

// ── Join Group via Invite Link ────────────────────────────────
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

// ── Get Invite Link ───────────────────────────────────────────
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

// ── Leave Group ───────────────────────────────────────────────
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

// ── Get Group Members ─────────────────────────────────────────
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

// ── Remove Member ─────────────────────────────────────────────
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

// ── Make Admin ────────────────────────────────────────────────
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

// ── Remove Admin ──────────────────────────────────────────────
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

// ── Set Approval Setting ──────────────────────────────────────
app.post("/group/approval", async (req, res) => {
  const { accountId, groupId, enabled } = req.body;
  if (!accountId || !groupId || enabled === undefined) return errRes(res, "Missing fields", 400);

  try {
    const sock = getSocket(accountId);
    if (!sock) return errRes(res, "Account not connected", 404);

    // Member approval setting
    await sock.groupMemberRequestUpdate(
      groupId,
      enabled ? "on" : "off"
    );
    res.json({ success: true, enabled });
  } catch (e) {
    // Fallback: some versions use different API
    try {
      const sock = getSocket(accountId);
      await sock.groupSettingUpdate(groupId, enabled ? "member_approval" : "no_member_approval");
      res.json({ success: true, enabled });
    } catch (e2) {
      errRes(res, e2.message);
    }
  }
});

// ── Get Pending Members ───────────────────────────────────────
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

// ── Reject Pending Member ─────────────────────────────────────
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

// ── Add Member to Group ───────────────────────────────────────
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

// ── Check if Number is on WhatsApp ───────────────────────────
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

// ── Restore saved sessions on startup ────────────────────────
async function restoreSessions() {
  if (!fs.existsSync(SESSIONS_ROOT)) return;

  const accountDirs = fs.readdirSync(SESSIONS_ROOT);
  for (const accountId of accountDirs) {
    const sessionDir = path.join(SESSIONS_ROOT, accountId);
    if (fs.statSync(sessionDir).isDirectory()) {
      const credsFile = path.join(sessionDir, "creds.json");
      if (fs.existsSync(credsFile)) {
        logger.info({ accountId }, "Restoring saved session...");
        try {
          await createConnection(accountId, false, null);
          logger.info({ accountId }, "Session restored");
        } catch (e) {
          logger.error({ accountId, err: e.message }, "Session restore failed");
        }
        await new Promise(r => setTimeout(r, 2000)); // Rate limit
      }
    }
  }
}

// ═══════════════════════════════════════════════════════════════
//                       START SERVER
// ═══════════════════════════════════════════════════════════════
app.listen(PORT, async () => {
  console.log(`
╔══════════════════════════════════════════════════════╗
║  WhatsApp Bridge Server - RUNNING                    ║
║  Port: ${PORT}                                          ║
║  Secret: ${SECRET.substring(0, 4)}...                         ║
╚══════════════════════════════════════════════════════╝
  `);
  logger.info({ port: PORT }, "Bridge server started");

  // Restore saved sessions
  await restoreSessions();
  logger.info("Session restore complete");
});

// Graceful shutdown
process.on("SIGINT", async () => {
  logger.info("Shutting down gracefully...");
  for (const [accountId, entry] of sockets.entries()) {
    try {
      // Don't logout, just end connection to preserve session
      entry.sock.end();
    } catch (e) {}
  }
  process.exit(0);
});
