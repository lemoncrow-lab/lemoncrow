// Rule test for src/lemoncrow/pro/capabilities/review/security_rules/javascript.yaml.
// Run it with `opengrep scan --test --config <rules dir> tests/fixtures/opengrep`. Nothing here runs: it is sample
// text for the scanner.
const child_process = require("child_process");
const crypto = require("crypto");
const fs = require("fs");
const path = require("path");
const serialize = require("node-serialize");
const axios = require("axios");
const vm = require("vm");

function dynamicCode(source, name) {
  // ruleid: lemoncrow.javascript.eval-dynamic
  eval(source);
  // ruleid: lemoncrow.javascript.eval-dynamic
  new Function("a", source);
  // ruleid: lemoncrow.javascript.eval-dynamic
  vm.runInNewContext(source);
  // ok: lemoncrow.javascript.eval-dynamic
  eval("1 + 1");
  // ok: lemoncrow.javascript.eval-dynamic
  return JSON.parse(name);
}

function commands(name) {
  // ruleid: lemoncrow.javascript.child-process-dynamic
  child_process.exec(`ls ${name}`);
  // ruleid: lemoncrow.javascript.child-process-dynamic
  child_process.execSync("cat " + name);
  // ok: lemoncrow.javascript.child-process-dynamic
  child_process.exec("ls -l");
  // ok: lemoncrow.javascript.child-process-dynamic
  child_process.execFile("ls", [name]);
}

function sql(db, id, name) {
  // ruleid: lemoncrow.javascript.sql-string-building
  db.query(`SELECT * FROM users WHERE id = ${id}`);
  // ruleid: lemoncrow.javascript.sql-string-building
  db.query("SELECT * FROM users WHERE name = '" + name + "'");
  // ok: lemoncrow.javascript.sql-string-building
  db.query("SELECT * FROM users WHERE id = $1", [id]);
  // ok: lemoncrow.javascript.sql-string-building
  db.query("SELECT " + "1");
}

function revive(data) {
  // ruleid: lemoncrow.javascript.unsafe-deserialization
  serialize.unserialize(data);
  // ok: lemoncrow.javascript.unsafe-deserialization
  JSON.parse(data);
}

function crypto_use(key, iv) {
  // ruleid: lemoncrow.javascript.weak-hash
  crypto.createHash("md5");
  // ruleid: lemoncrow.javascript.weak-hash
  crypto.createHash("sha1");
  // ok: lemoncrow.javascript.weak-hash
  crypto.createHash("sha256");
  // ruleid: lemoncrow.javascript.weak-cipher
  crypto.createCipher("aes192", key);
  // ruleid: lemoncrow.javascript.weak-cipher
  crypto.createCipheriv("aes-128-ecb", key, null);
  // ruleid: lemoncrow.javascript.weak-cipher
  crypto.createCipheriv("des-ede3-cbc", key, iv);
  // ok: lemoncrow.javascript.weak-cipher
  crypto.createCipheriv("aes-256-gcm", key, iv);
}

function tls(url) {
  // ruleid: lemoncrow.javascript.tls-verification-disabled
  axios.get(url, { httpsAgent: { rejectUnauthorized: false } });
  // ruleid: lemoncrow.javascript.tls-verification-disabled
  process.env.NODE_TLS_REJECT_UNAUTHORIZED = "0";
  // ok: lemoncrow.javascript.tls-verification-disabled
  axios.get(url, { timeout: 5000 });
}

function handlers(app) {
  app.get("/proxy", (req, res) => {
    const target = req.query.url;
    // ruleid: lemoncrow.javascript.ssrf-user-controlled-url
    axios.get(target);
    // ok: lemoncrow.javascript.ssrf-user-controlled-url
    axios.get("https://api.example.invalid/status");
    res.end();
  });
  app.get("/file", (req, res) => {
    const name = req.query.name;
    // ruleid: lemoncrow.javascript.path-traversal-user-input
    fs.readFile(path.join("/srv/files", name), () => {});
    // ruleid: lemoncrow.javascript.path-traversal-user-input
    res.sendFile(name);
    // ok: lemoncrow.javascript.path-traversal-user-input
    fs.readFile(path.join("/srv/files", path.basename(name)), () => {});
    // ok: lemoncrow.javascript.path-traversal-user-input
    fs.readFile("/srv/files/fixed.txt", () => {});
  });
}

module.exports = { dynamicCode, commands, sql, revive, crypto_use, tls, handlers };
