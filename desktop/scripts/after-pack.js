// electron-builder afterPack hook: give the packed app permissions that suit an
// installed program, whatever the checkout had. A folder only its owner may
// enter would keep every other account on the machine from running the app.
const fs = require("node:fs");
const path = require("node:path");

function open(target) {
  const info = fs.lstatSync(target);
  if (info.isSymbolicLink()) return;
  const mode = info.mode & 0o777;
  if (info.isDirectory()) {
    if (mode !== 0o755) fs.chmodSync(target, 0o755);
    for (const name of fs.readdirSync(target)) open(path.join(target, name));
  } else {
    // Readable by all; runnable by all if its owner may run it.
    const wanted = mode & 0o100 ? 0o755 : 0o644;
    if (mode !== wanted) fs.chmodSync(target, wanted);
  }
}

exports.default = async function afterPack(context) {
  if (process.platform === "win32") return; // no such permissions there
  open(context.appOutDir);
};
