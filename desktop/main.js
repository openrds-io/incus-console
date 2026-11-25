// Incus Console desktop runtime, on pgAdmin's model: start the Python server
// on a free local port with a random key, then show it in a window.
//
// Which Python and which server code:
//   packaged app   resources/python (bundled) and resources/server
//   development    the repository's .venv and the repository itself
//   override       INCUS_CONSOLE_PYTHON=/path/to/python
"use strict";

const { app, BrowserWindow, Menu, dialog, shell } = require("electron");
const { spawn } = require("node:child_process");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const readline = require("node:readline");

const STARTUP_TIMEOUT_MS = 30000;
const isWindows = process.platform === "win32";

let server = null; // the Python child process
let origin = null; // http://127.0.0.1:<port>
let mainWindow = null;
let quitting = false;

function serverDir() {
  return app.isPackaged
    ? path.join(process.resourcesPath, "server")
    : path.resolve(__dirname, "..");
}

function pythonPath() {
  if (process.env.INCUS_CONSOLE_PYTHON) return process.env.INCUS_CONSOLE_PYTHON;
  const root = app.isPackaged
    ? path.join(process.resourcesPath, "python")
    : path.resolve(__dirname, "..", ".venv");
  const bundled = app.isPackaged;
  const candidate = isWindows
    ? path.join(root, bundled ? "python.exe" : path.join("Scripts", "python.exe"))
    : path.join(root, "bin", "python3");
  return fs.existsSync(candidate) ? candidate : isWindows ? "python" : "python3";
}

function logFile() {
  const dir = path.join(app.getPath("userData"), "logs");
  fs.mkdirSync(dir, { recursive: true });
  return path.join(dir, "server.log");
}

function tail(file, lines = 20) {
  try {
    return fs.readFileSync(file, "utf8").trimEnd().split("\n").slice(-lines).join("\n");
  } catch {
    return "";
  }
}

function startServer(key) {
  const log = logFile();
  const out = fs.createWriteStream(log, { flags: "w" });
  const dataDir = path.join(app.getPath("userData"), "data");
  const child = spawn(pythonPath(), ["-m", "app.desktop"], {
    cwd: serverDir(),
    env: {
      ...process.env,
      INCUS_CONSOLE_DESKTOP_KEY: key,
      INCUS_CONSOLE_DATA_DIR: dataDir,
      PYTHONDONTWRITEBYTECODE: "1",
      PYTHONUNBUFFERED: "1",
    },
    stdio: ["ignore", "pipe", "pipe"],
    windowsHide: true,
  });
  child.stderr.pipe(out);
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      reject(new Error("The server did not start within 30 seconds."));
    }, STARTUP_TIMEOUT_MS);
    const lines = readline.createInterface({ input: child.stdout });
    lines.on("line", (line) => {
      out.write(line + "\n");
      const match = /^LISTENING (\d+)$/.exec(line.trim());
      if (match) {
        clearTimeout(timer);
        resolve({ child, port: Number(match[1]) });
      }
    });
    child.on("error", (error) => {
      clearTimeout(timer);
      reject(new Error(`Could not run ${pythonPath()}: ${error.message}`));
    });
    child.on("exit", (code) => {
      clearTimeout(timer);
      reject(new Error(`The server stopped (exit code ${code}).`));
    });
  });
}

function isOurs(url) {
  try {
    return new URL(url).origin === origin;
  } catch {
    return false;
  }
}

function openOutside(url) {
  if (/^https?:\/\//.test(url)) shell.openExternal(url);
}

function createWindow() {
  const win = new BrowserWindow({
    width: 1280,
    height: 860,
    minWidth: 720,
    minHeight: 480,
    title: "Incus Console",
    backgroundColor: "#262626",
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  // Links to other sites open in the user's browser, never inside the app.
  win.webContents.setWindowOpenHandler(({ url }) => {
    openOutside(url);
    return { action: "deny" };
  });
  win.webContents.on("will-navigate", (event, url) => {
    if (!isOurs(url) && !url.startsWith("file:")) {
      event.preventDefault();
      openOutside(url);
    }
  });
  win.on("closed", () => {
    if (mainWindow === win) mainWindow = null;
  });
  return win;
}

function buildMenu() {
  const template = [
    ...(process.platform === "darwin" ? [{ role: "appMenu" }] : []),
    {
      label: "File",
      submenu: [
        { label: "Server log", click: () => shell.openPath(logFile()) },
        { label: "Data folder", click: () => shell.openPath(app.getPath("userData")) },
        { type: "separator" },
        process.platform === "darwin" ? { role: "close" } : { role: "quit" },
      ],
    },
    { role: "editMenu" },
    {
      label: "View",
      submenu: [
        { role: "reload" },
        { type: "separator" },
        { role: "resetZoom" },
        { role: "zoomIn" },
        { role: "zoomOut" },
        { type: "separator" },
        { role: "togglefullscreen" },
        ...(app.isPackaged ? [] : [{ role: "toggleDevTools" }]),
      ],
    },
    { role: "windowMenu" },
    {
      role: "help",
      submenu: [
        {
          label: "incus-console on GitHub",
          click: () => shell.openExternal("https://github.com/openrds-io/incus-console"),
        },
        {
          label: "Incus documentation",
          click: () => shell.openExternal("https://linuxcontainers.org/incus/docs/main/"),
        },
      ],
    },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

async function launch() {
  buildMenu();
  mainWindow = createWindow();
  mainWindow.loadFile(path.join(__dirname, "loading.html"));
  const key = crypto.randomBytes(32).toString("base64url");
  try {
    const started = await startServer(key);
    server = started.child;
    origin = `http://127.0.0.1:${started.port}`;
  } catch (error) {
    dialog.showErrorBox(
      "Incus Console could not start",
      `${error.message}\n\n${tail(logFile())}\n\nThe full log is ${logFile()}`,
    );
    app.exit(1);
    return;
  }
  server.on("exit", (code) => {
    if (quitting) return;
    dialog.showErrorBox(
      "Incus Console stopped",
      `The server stopped unexpectedly (exit code ${code}).\n\n${tail(logFile())}`,
    );
    app.exit(1);
  });
  await mainWindow.loadURL(`${origin}/?key=${key}`);
}

function stopServer() {
  quitting = true;
  if (server && server.exitCode === null) server.kill();
}

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.focus();
    }
  });
  app.whenReady().then(launch);
  app.on("activate", () => {
    // macOS: clicking the dock icon after closing the window reopens it.
    if (!mainWindow && origin) {
      mainWindow = createWindow();
      mainWindow.loadURL(`${origin}/`);
    }
  });
  app.on("window-all-closed", () => {
    if (process.platform !== "darwin") app.quit();
  });
  app.on("before-quit", stopServer);
}
