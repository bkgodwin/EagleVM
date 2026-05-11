const form = document.getElementById("launch-form");
const statusEl = document.getElementById("status");
const launch = document.getElementById("launch");
const wrap = document.getElementById("terminal-wrap");
const terminalEl = document.getElementById("terminal");
const containerName = document.getElementById("container-name");
const containerIp = document.getElementById("container-ip");
const helpButton = document.getElementById("help-button");
const helpModal = document.getElementById("help-modal");
const helpClose = document.getElementById("help-close");
const helpHeaderClose = document.getElementById("help-header-close");

// GUI mode elements
const guiWrap = document.getElementById("gui-wrap");
const vncCanvas = document.getElementById("vnc-canvas");
const guiContainerName = document.getElementById("gui-container-name");
const guiContainerIp = document.getElementById("gui-container-ip");
const guiUsernameDisplay = document.getElementById("gui-username-display");
const guiPasswordDisplay = document.getElementById("gui-password-display");
const credentialsModal = document.getElementById("credentials-modal");
const credsUsername = document.getElementById("creds-username");
const credsPassword = document.getElementById("creds-password");
const credsConnectBtn = document.getElementById("creds-connect-btn");

function closeHelp() {
  helpModal.hidden = true;
  helpButton.focus();
}

helpButton.addEventListener("click", () => {
  helpModal.hidden = false;
  helpHeaderClose.focus();
});

helpClose.addEventListener("click", closeHelp);
helpHeaderClose.addEventListener("click", closeHelp);

helpModal.addEventListener("click", (event) => {
  if (event.target === helpModal) closeHelp();
});

window.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !helpModal.hidden) closeHelp();
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = form.querySelector("button");
  button.disabled = true;
  statusEl.textContent = "Creating session...";

  try {
    const response = await fetch("/api/session", {
      method: "POST",
      headers: {"Content-Type": "application/json"}
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Unable to launch session.");
    if (data.is_gui) {
      showCredentialsModal(data, () => openGUI(data));
    } else {
      openTerminal(data);
    }
  } catch (error) {
    statusEl.textContent = error.message;
    button.disabled = false;
  }
});

function showCredentialsModal(session, onConnect) {
  credsUsername.textContent = session.gui_vm_username || "(not set)";
  credsPassword.textContent = session.gui_vm_password || "(not set)";
  credentialsModal.hidden = false;
  credsConnectBtn.focus();

  function handleConnect() {
    credentialsModal.hidden = true;
    credsConnectBtn.removeEventListener("click", handleConnect);
    onConnect();
  }

  credsConnectBtn.addEventListener("click", handleConnect);
}

async function openGUI(session) {
  launch.hidden = true;
  guiWrap.hidden = false;

  guiContainerName.textContent = `Container: ${session.hostname}`;
  guiContainerIp.textContent = `IPv4: ${session.ip}`;
  if (session.gui_vm_username) {
    guiUsernameDisplay.textContent = `User: ${session.gui_vm_username}`;
  }
  if (session.gui_vm_password) {
    guiPasswordDisplay.textContent = `Pass: ${session.gui_vm_password}`;
  }

  let RFB;
  try {
    const mod = await import("https://cdn.jsdelivr.net/npm/@novnc/novnc@1.4.0/core/rfb.js");
    RFB = mod.default;
  } catch (err) {
    guiContainerName.textContent = `Failed to load VNC client: ${err.message}`;
    return;
  }

  const protocol = location.protocol === "https:" ? "wss" : "ws";
  const wsUrl = `${protocol}://${location.host}/ws-gui/${session.session_id}`;

  const rfb = new RFB(vncCanvas, wsUrl);
  rfb.scaleViewport = true;
  rfb.resizeSession = false;
  rfb.viewOnly = false;

  rfb.addEventListener("connect", () => {
    guiContainerName.textContent = `Container: ${session.hostname}`;
  });

  rfb.addEventListener("disconnect", (e) => {
    const detail = e.detail || {};
    const reason = detail.clean ? "Session disconnected." : "Session disconnected unexpectedly.";
    guiContainerName.textContent = reason + " Relaunch to start a new session.";
  });
}

function openTerminal(session) {
  launch.hidden = true;
  wrap.hidden = false;
  containerName.textContent = `Container: ${session.hostname}`;
  containerIp.textContent = `IPv4: ${session.ip}`;
  terminalEl.replaceChildren();

  const terminal = new Terminal({
    cursorBlink: true,
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
    fontSize: 15,
    theme: {background: "#000000", foreground: "#d7ffe7", cursor: "#64ff95"}
  });
  const fit = new FitAddon.FitAddon();
  terminal.loadAddon(fit);
  terminal.open(terminalEl);

  const protocol = location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${protocol}://${location.host}/ws/${session.session_id}`);
  socket.binaryType = "arraybuffer";
  let resizeFrame = null;
  let resizeTimer = null;

  function sendResize() {
    fit.fit();
    if (socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify({
        type: "resize",
        cols: terminal.cols,
        rows: terminal.rows
      }));
    }
  }

  function scheduleResize() {
    if (resizeFrame !== null) return;
    resizeFrame = requestAnimationFrame(() => {
      resizeFrame = null;
      sendResize();
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(sendResize, 75);
    });
  }

  function cleanup() {
    observer.disconnect();
    window.removeEventListener("resize", scheduleResize);
    window.removeEventListener("beforeunload", closeSocket);
    clearTimeout(resizeTimer);
    if (resizeFrame !== null) {
      cancelAnimationFrame(resizeFrame);
      resizeFrame = null;
    }
  }

  function closeSocket() {
    socket.close();
  }

  socket.addEventListener("open", () => {
    terminal.focus();
    scheduleResize();
  });
  socket.addEventListener("message", (event) => {
    terminal.write(typeof event.data === "string" ? event.data : new Uint8Array(event.data));
  });
  socket.addEventListener("close", () => {
    cleanup();
    terminal.writeln("\\r\\nSession disconnected. Reopen or relaunch within the reconnect window to resume.");
  });

  terminal.onData((data) => {
    if (socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify({type: "input", data}));
    }
  });
  const observer = new ResizeObserver(scheduleResize);
  // Observe the stable wrapper so fit() does not retrigger the observer on xterm's own DOM updates.
  observer.observe(wrap);
  window.addEventListener("resize", scheduleResize);
  window.addEventListener("beforeunload", closeSocket);
  scheduleResize();
}
