// The browser terminal: xterm.js on a websocket to this app, which relays to Incus.
// We send {"t":"i","d":keys} and {"t":"r","c":cols,"r":rows}. Output arrives as
// binary frames, then {"t":"x"} when the shell ends or {"t":"e","m":message}
// if it could not start.
(() => {
  const el = document.getElementById("term");
  const state = document.getElementById("term-state");
  const again = document.getElementById("term-again");
  if (!el) return;
  if (!window.Terminal || !window.FitAddon) {
    state.textContent = "Terminal files did not load";
    state.className = "badge status-error";
    return;
  }

  const term = new Terminal({
    cursorBlink: true,
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
    fontSize: 14,
    scrollback: 5000,
    theme: { background: "#0b1220" },
  });
  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open(el);
  fit.fit();
  let socket = null;

  const show = (text, kind) => {
    state.textContent = text;
    state.className = "badge" + (kind ? " status-" + kind : "");
  };
  const send = (message) => {
    if (socket && socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify(message));
  };

  function connect() {
    again.hidden = true;
    show("Connecting", "pending");
    const scheme = location.protocol === "https:" ? "wss://" : "ws://";
    const query = "?cols=" + term.cols + "&rows=" + term.rows;
    let ended = false;
    socket = new WebSocket(scheme + location.host + el.dataset.ws + query);
    socket.binaryType = "arraybuffer";
    socket.onopen = () => {
      show("Connected", "running");
      term.focus();
    };
    socket.onmessage = (event) => {
      if (typeof event.data !== "string") {
        term.write(new Uint8Array(event.data));
        return;
      }
      let message = {};
      try {
        message = JSON.parse(event.data);
      } catch (error) {
        return;
      }
      if (message.t === "x") {
        ended = true;
        show("Session ended", "");
        term.write("\r\n[session ended]\r\n");
      }
      if (message.t === "e") {
        ended = true;
        show("Could not connect", "error");
        term.write("\r\n" + message.m + "\r\n");
      }
    };
    socket.onclose = () => {
      if (!ended) {
        show("Disconnected", "error");
        term.write("\r\n[disconnected]\r\n");
      }
      again.hidden = false;
    };
  }

  term.onData((keys) => send({ t: "i", d: keys }));
  term.onResize((size) => send({ t: "r", c: size.cols, r: size.rows }));
  window.addEventListener("resize", () => fit.fit());
  again.addEventListener("click", () => {
    term.reset();
    connect();
  });
  connect();
})();
