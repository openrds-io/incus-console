// Put a standalone Python 3.13 with this app's dependencies in desktop/python-dist,
// for electron-builder to ship as resources/python.
//
// The interpreter comes from python-build-standalone (the builds uv uses). Run it
// on each OS you build for: the bundle matches the machine that runs this script.
//
//   node scripts/bundle-python.mjs            newest 3.13 in the latest release
//   PBS_TAG=20261101 node scripts/...         a specific release
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const PYTHON_MINOR = "3.13";
const REPO = "astral-sh/python-build-standalone";
const here = path.dirname(fileURLToPath(import.meta.url));
const desktop = path.resolve(here, "..");
const target = path.join(desktop, "python-dist");
const requirements = path.resolve(desktop, "..", "requirements.txt");

const TRIPLES = {
  "darwin-arm64": "aarch64-apple-darwin",
  "darwin-x64": "x86_64-apple-darwin",
  "linux-x64": "x86_64-unknown-linux-gnu",
  "linux-arm64": "aarch64-unknown-linux-gnu",
  "win32-x64": "x86_64-pc-windows-msvc",
  "win32-arm64": "aarch64-pc-windows-msvc",
};

async function getJSON(url) {
  const response = await fetch(url, { headers: { "User-Agent": "incus-console-build" } });
  if (!response.ok) throw new Error(`${url}: HTTP ${response.status}`);
  return response.json();
}

async function getBytes(url) {
  const response = await fetch(url, { headers: { "User-Agent": "incus-console-build" } });
  if (!response.ok) throw new Error(`${url}: HTTP ${response.status}`);
  return Buffer.from(await response.arrayBuffer());
}

async function main() {
  const triple = TRIPLES[`${process.platform}-${process.arch}`];
  if (!triple) throw new Error(`No Python build for ${process.platform}-${process.arch}.`);

  const tag =
    process.env.PBS_TAG ||
    (await getJSON(`https://raw.githubusercontent.com/${REPO}/latest-release/latest-release.json`))
      .tag;
  const release = await getJSON(`https://api.github.com/repos/${REPO}/releases/tags/${tag}`);
  const pattern = new RegExp(
    `^cpython-${PYTHON_MINOR.replace(".", "\\.")}\\.(\\d+)\\+${tag}-${triple}-install_only_stripped\\.tar\\.gz$`,
  );
  const asset = release.assets
    .filter((a) => pattern.test(a.name))
    .sort((a, b) => Number(pattern.exec(b.name)[1]) - Number(pattern.exec(a.name)[1]))[0];
  if (!asset) throw new Error(`Release ${tag} has no Python ${PYTHON_MINOR} for ${triple}.`);

  console.log(`Downloading ${asset.name}`);
  const archive = await getBytes(asset.browser_download_url);
  const sums = release.assets.find((a) => a.name === "SHA256SUMS");
  if (!sums) throw new Error(`Release ${tag} has no SHA256SUMS to check the download against.`);
  const expected = (await getBytes(sums.browser_download_url))
    .toString()
    .split("\n")
    .map((line) => line.trim().split(/\s+/))
    .find(([, name]) => name === asset.name)?.[0];
  const actual = createHash("sha256").update(archive).digest("hex");
  if (!expected || expected !== actual) {
    throw new Error(`Checksum mismatch for ${asset.name}: expected ${expected}, got ${actual}.`);
  }

  const work = fs.mkdtempSync(path.join(os.tmpdir(), "incus-console-python-"));
  const tarball = path.join(work, asset.name);
  fs.writeFileSync(tarball, archive);
  execFileSync("tar", ["-xzf", tarball, "-C", work], { stdio: "inherit" });
  fs.rmSync(target, { recursive: true, force: true });
  fs.renameSync(path.join(work, "python"), target);
  fs.rmSync(work, { recursive: true, force: true });

  const python =
    process.platform === "win32"
      ? path.join(target, "python.exe")
      : path.join(target, "bin", "python3");
  console.log(`Installing ${path.relative(desktop, requirements)}`);
  execFileSync(
    python,
    ["-m", "pip", "install", "--no-cache-dir", "--disable-pip-version-check", "-r", requirements],
    { stdio: "inherit" },
  );
  console.log(`Python ready in ${path.relative(process.cwd(), target) || target}`);
}

main().catch((error) => {
  console.error(error.message);
  process.exit(1);
});
