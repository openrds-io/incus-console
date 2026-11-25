// Record the commit being packaged in app/COMMIT, which the About box shows.
// Adds "release" when the checkout is exactly the tag for this version.
import { execFileSync } from "node:child_process";
import { readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const git = (...args) => {
  try {
    return execFileSync("git", ["-C", root, ...args], { encoding: "utf8" }).trim();
  } catch {
    return "";
  }
};
const { version } = JSON.parse(readFileSync(join(root, "desktop", "package.json"), "utf8"));
const commit = git("rev-parse", "--short=12", "HEAD") || "unknown";
const tagged = git("describe", "--tags", "--exact-match") === `v${version}`;
const clean = git("status", "--porcelain", "--untracked-files=no") === "";
writeFileSync(join(root, "app", "COMMIT"), `${commit}${tagged && clean ? " release" : ""}\n`);
console.log(`app/COMMIT: ${commit}${tagged && clean ? " (release)" : ""}`);
