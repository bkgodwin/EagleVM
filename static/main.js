const form = document.getElementById("launch-form");
const statusEl = document.getElementById("status");
const launch = document.getElementById("launch");
const wrap = document.getElementById("terminal-wrap");
const terminalEl = document.getElementById("terminal");
const containerName = document.getElementById("container-name");
const containerIp = document.getElementById("container-ip");

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
    openTerminal(data);
  } catch (error) {
    statusEl.textContent = error.message;
    button.disabled = false;
  }
});

function openTerminal(session) {
  launch.hidden = true;
  wrap.hidden = false;
  containerName.textContent = `Container: ${session.hostname}`;
  containerIp.textContent = `IPv4: ${session.ip}`;

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
    requestAnimationFrame(() => {
      sendResize();
      setTimeout(sendResize, 75);
    });
  }

  socket.addEventListener("open", () => {
    terminal.focus();
    scheduleResize();
  });
  socket.addEventListener("message", (event) => {
    terminal.write(typeof event.data === "string" ? event.data : new Uint8Array(event.data));
  });
  socket.addEventListener("close", () => {
    terminal.writeln("\\r\\nSession closed. Temporary container cleanup requested.");
  });

  terminal.onData((data) => {
    if (socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify({type: "input", data}));
    }
  });
  const observer = new ResizeObserver(scheduleResize);
  observer.observe(terminalEl);
  window.addEventListener("resize", scheduleResize);
  window.addEventListener("beforeunload", () => socket.close());
  scheduleResize();
}