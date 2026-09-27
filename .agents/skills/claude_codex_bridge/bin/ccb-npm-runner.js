"use strict";

const childProcess = require("child_process");
const { executablePath, install } = require("./ccb-npm-install");
const path = require("path");

const packageRoot = path.resolve(__dirname, "..");
const manifest = require(path.join(packageRoot, "package.json"));

function npmManagedEnvironment(baseEnv = process.env) {
  return {
    ...baseEnv,
    CCB_INSTALL_KIND: "npm",
    CCB_NPM_PACKAGE_NAME: manifest.name,
    CCB_NPM_PACKAGE_ROOT: packageRoot,
    CCB_NPM_PACKAGE_VERSION: manifest.version,
  };
}

async function run(command) {
  await install();
  const target = executablePath(command);
  const child = childProcess.spawn(target, process.argv.slice(2), {
    stdio: "inherit",
    env: npmManagedEnvironment(),
  });
  child.on("error", (error) => {
    console.error(error.message || error);
    process.exit(1);
  });
  child.on("exit", (code, signal) => {
    if (signal) {
      process.kill(process.pid, signal);
      return;
    }
    process.exit(code === null ? 1 : code);
  });
}

module.exports = {
  npmManagedEnvironment,
  run,
};
