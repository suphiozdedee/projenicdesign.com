#!/usr/bin/env node
"use strict";

const childProcess = require("child_process");
const crypto = require("crypto");
const fs = require("fs");
const https = require("https");
const os = require("os");
const path = require("path");

const root = path.resolve(__dirname, "..");
const manifest = require(path.join(root, "package.json"));
const version = manifest.version;
const vendorRoot = path.join(root, ".ccb-release");
const installLock = path.join(root, ".ccb-install.lock");
const runtimeProbe = [
  "import sys",
  "if sys.version_info < (3, 10):",
  "    raise SystemExit(1)",
  "try:",
  "    import tomllib",
  "except ModuleNotFoundError:",
  "    import tomli",
  "import aiohttp",
  "from cryptography.hazmat.primitives.asymmetric import ed25519, x25519",
  "from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305",
].join("\n");

function artifactForHost() {
  if (process.platform === "darwin") {
    return {
      directory: "ccb-macos-universal",
      file: "ccb-macos-universal.tar.gz",
    };
  }
  if (process.platform === "linux" && process.arch === "x64") {
    return {
      directory: "ccb-linux-x86_64",
      file: "ccb-linux-x86_64.tar.gz",
    };
  }
  throw new Error(
    `Unsupported platform for @seemseam/ccb: ${process.platform}/${process.arch}. ` +
      "Use Linux x64, macOS x64, macOS arm64, or install the GitHub release manually."
  );
}

function installDir(info) {
  return path.join(vendorRoot, info.directory);
}

function executablePath(command = "ccb") {
  const info = artifactForHost();
  const base = installDir(info);
  return command === "ccb" ? path.join(base, "ccb") : path.join(base, "bin", command);
}

function runtimePythonPath(info) {
  return path.join(installDir(info), ".venv", "bin", "python");
}

function isReleaseInstalled(info) {
  const dir = installDir(info);
  const versionFile = path.join(dir, "VERSION");
  const ccbPath = path.join(dir, "ccb");
  if (!fs.existsSync(versionFile) || !fs.existsSync(ccbPath)) {
    return false;
  }
  try {
    fs.accessSync(ccbPath, fs.constants.X_OK);
    return fs.readFileSync(versionFile, "utf8").trim() === version;
  } catch (_error) {
    return false;
  }
}

function isRuntimeReady(info) {
  const pythonPath = runtimePythonPath(info);
  if (!fs.existsSync(pythonPath)) {
    return false;
  }
  const completed = childProcess.spawnSync(pythonPath, ["-c", runtimeProbe], {
    stdio: "ignore",
    timeout: 15000,
  });
  return !completed.error && completed.status === 0;
}

function isInstalled(info) {
  return isReleaseInstalled(info) && isRuntimeReady(info);
}

function download(url, destination, redirects = 0) {
  if (redirects > 5) {
    throw new Error(`Too many redirects while downloading ${url}`);
  }
  return new Promise((resolve, reject) => {
    const request = https.get(url, (response) => {
      const status = response.statusCode || 0;
      if ([301, 302, 303, 307, 308].includes(status) && response.headers.location) {
        response.resume();
        const redirected = new URL(response.headers.location, url).toString();
        download(redirected, destination, redirects + 1).then(resolve, reject);
        return;
      }
      if (status < 200 || status >= 300) {
        response.resume();
        reject(new Error(`Download failed for ${url}: HTTP ${status}`));
        return;
      }
      const file = fs.createWriteStream(destination);
      response.pipe(file);
      file.on("finish", () => file.close(resolve));
      file.on("error", reject);
    });
    request.on("error", reject);
  });
}

function parseSha256Sums(text) {
  const checksums = new Map();
  for (const line of text.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed) {
      continue;
    }
    const match = trimmed.match(/^([a-fA-F0-9]{64})\s+\*?(.+)$/);
    if (match) {
      checksums.set(path.basename(match[2]), match[1].toLowerCase());
    }
  }
  return checksums;
}

function sha256File(filePath) {
  const hash = crypto.createHash("sha256");
  hash.update(fs.readFileSync(filePath));
  return hash.digest("hex");
}

function run(command, args, options = {}) {
  const completed = childProcess.spawnSync(command, args, {
    stdio: "inherit",
    ...options,
  });
  if (completed.error) {
    throw completed.error;
  }
  if (completed.status !== 0) {
    throw new Error(`${command} ${args.join(" ")} failed with exit ${completed.status}`);
  }
}

function bootstrapRuntime(info) {
  const dir = installDir(info);
  const installerPath = path.join(dir, "install.sh");
  if (!isReleaseInstalled(info)) {
    throw new Error(`Cannot bootstrap an incomplete CCB release at ${dir}`);
  }
  if (!fs.existsSync(installerPath)) {
    throw new Error(`CCB runtime bootstrap installer not found at ${installerPath}`);
  }

  const env = {
    ...process.env,
    CODEX_INSTALL_PREFIX: dir,
    CODEX_BIN_DIR: path.join(dir, ".npm-runtime-bin"),
    CCB_SOURCE_KIND: "release",
    CCB_USE_MANAGED_VENV: "1",
    CCB_INSTALL_TOMLI: "1",
    CCB_INSTALL_MOBILE_RELAY_DEPS: "1",
    CCB_INSTALL_ROLES: "0",
    CCB_INSTALL_NEOVIM: "0",
  };
  run("bash", [installerPath, "runtime-bootstrap"], { env });
  if (!isRuntimeReady(info)) {
    throw new Error(`CCB managed Python runtime is not usable at ${runtimePythonPath(info)}`);
  }
}

function sleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function processIsAlive(pid) {
  if (!Number.isInteger(pid) || pid <= 0) {
    return false;
  }
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return error && error.code === "EPERM";
  }
}

function readLockOwner() {
  try {
    return JSON.parse(fs.readFileSync(path.join(installLock, "owner.json"), "utf8"));
  } catch (_error) {
    return null;
  }
}

function reclaimStaleInstallLock() {
  const owner = readLockOwner();
  if (owner && processIsAlive(Number(owner.pid))) {
    return false;
  }
  try {
    const ageMs = Date.now() - fs.statSync(installLock).mtimeMs;
    if (!owner && ageMs < 30000) {
      return false;
    }
    fs.rmSync(installLock, { recursive: true, force: true });
    return true;
  } catch (_error) {
    return false;
  }
}

async function acquireInstallLock() {
  const configuredTimeout = Number(process.env.CCB_NPM_INSTALL_LOCK_TIMEOUT_MS);
  const timeoutMs =
    Number.isFinite(configuredTimeout) && configuredTimeout > 0
      ? configuredTimeout
      : 15 * 60 * 1000;
  const deadline = Date.now() + timeoutMs;
  const token = crypto.randomBytes(16).toString("hex");

  while (true) {
    try {
      fs.mkdirSync(installLock);
    } catch (error) {
      if (!error || error.code !== "EEXIST") {
        throw error;
      }
      if (reclaimStaleInstallLock()) {
        continue;
      }
      if (Date.now() >= deadline) {
        throw new Error(`Timed out waiting for CCB npm install lock: ${installLock}`);
      }
      await sleep(200);
      continue;
    }

    try {
      fs.writeFileSync(
        path.join(installLock, "owner.json"),
        JSON.stringify({ pid: process.pid, token, createdAt: new Date().toISOString() })
      );
    } catch (error) {
      fs.rmSync(installLock, { recursive: true, force: true });
      throw error;
    }
    return () => {
      const owner = readLockOwner();
      if (owner && owner.token === token) {
        fs.rmSync(installLock, { recursive: true, force: true });
      }
    };
  }
}

async function downloadRelease(info) {
  const baseUrl =
    process.env.CCB_NPM_RELEASE_BASE_URL ||
    `https://github.com/SeemSeam/claude_codex_bridge/releases/download/v${version}`;
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "ccb-npm-"));
  const archivePath = path.join(tmpDir, info.file);
  const sumsPath = path.join(tmpDir, "SHA256SUMS");

  try {
    await download(`${baseUrl}/${info.file}`, archivePath);
    await download(`${baseUrl}/SHA256SUMS`, sumsPath);
    const checksums = parseSha256Sums(fs.readFileSync(sumsPath, "utf8"));
    const expected = checksums.get(info.file);
    if (!expected) {
      throw new Error(`SHA256SUMS does not contain ${info.file}`);
    }
    const actual = sha256File(archivePath);
    if (actual !== expected) {
      throw new Error(`Checksum mismatch for ${info.file}: expected ${expected}, got ${actual}`);
    }

    fs.rmSync(vendorRoot, { recursive: true, force: true });
    fs.mkdirSync(vendorRoot, { recursive: true });
    run("tar", ["-xzf", archivePath, "-C", vendorRoot]);
    if (!fs.existsSync(executablePath("ccb"))) {
      throw new Error(`Installed CCB executable not found at ${executablePath("ccb")}`);
    }
    console.log(`Installed CCB v${version} from ${info.file}.`);
  } finally {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  }
}

async function install() {
  const info = artifactForHost();
  if (isInstalled(info)) {
    return;
  }

  const releaseLock = await acquireInstallLock();
  try {
    if (isInstalled(info)) {
      return;
    }
    if (!isReleaseInstalled(info)) {
      if (process.env.CCB_NPM_SKIP_DOWNLOAD === "1") {
        console.warn("Skipping CCB release download because CCB_NPM_SKIP_DOWNLOAD=1.");
        return;
      }
      await downloadRelease(info);
    }
    if (!isRuntimeReady(info)) {
      bootstrapRuntime(info);
    }
  } finally {
    releaseLock();
  }
}

if (require.main === module) {
  install().catch((error) => {
    console.error(error.message || error);
    process.exit(1);
  });
}

module.exports = {
  artifactForHost,
  bootstrapRuntime,
  executablePath,
  install,
  installDir,
  isInstalled,
  isReleaseInstalled,
  isRuntimeReady,
  runtimePythonPath,
};
