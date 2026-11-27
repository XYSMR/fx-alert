// node --test site/tests/*.test.js
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const kg = require("../../scripts/vapid_keygen.js");

test("產生的金鑰是一對：私鑰 43 字、公鑰 87 字且 B 開頭（site-config 的檢查）", () => {
  const { privateKey, publicKey } = kg.generate();
  assert.match(privateKey, /^[A-Za-z0-9_-]{43}$/);
  assert.match(publicKey, /^B[A-Za-z0-9_-]{86}$/);
  const ecdh = crypto.createECDH("prime256v1");
  ecdh.setPrivateKey(Buffer.from(privateKey, "base64url"));
  assert.equal(ecdh.getPublicKey().toString("base64url"), publicKey);
  assert.notEqual(kg.generate().privateKey, privateKey);
});

test("私鑰只寫進檔案（權限 600、不覆蓋），畫面上只印公鑰", (t) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "vapid-"));
  const file = path.join(dir, "test.vapid");
  const out = [];
  t.mock.method(console, "log", (...a) => out.push(a.join(" ")));
  t.mock.method(console, "error", (...a) => out.push(a.join(" ")));
  assert.equal(kg.main([file]), 0);
  const priv = fs.readFileSync(file, "utf8").trim();
  assert.ok(!out.join("\n").includes(priv), "畫面上不能出現私鑰");
  if (process.platform !== "win32") assert.equal(fs.statSync(file).mode & 0o777, 0o600);
  assert.equal(kg.main([file]), 1);
  assert.equal(fs.readFileSync(file, "utf8").trim(), priv, "已存在的檔案不能被覆蓋");
  assert.equal(kg.main([]), 2);
  fs.rmSync(dir, { recursive: true });
});
