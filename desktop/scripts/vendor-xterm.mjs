// Copy xterm.js (MIT) from node_modules into the app's static files, so the
// browser terminal works offline and without a CDN. Run after `npm install`;
// commit the result.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const desktop = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const modules = path.join(desktop, "node_modules", "@xterm");
const target = path.resolve(desktop, "..", "app", "static", "vendor", "xterm");

const files = [
  ["xterm/lib/xterm.js", "xterm.js"],
  ["xterm/css/xterm.css", "xterm.css"],
  ["xterm/LICENSE", "LICENSE"],
  ["addon-fit/lib/addon-fit.js", "addon-fit.js"],
];

fs.mkdirSync(target, { recursive: true });
for (const [from, to] of files) {
  fs.copyFileSync(path.join(modules, from), path.join(target, to));
  console.log(`app/static/vendor/xterm/${to}`);
}
