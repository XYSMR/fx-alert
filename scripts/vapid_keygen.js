// 產生手機推播用的 VAPID 金鑰（P-256）。只用 Node 內建的 crypto，不需要安裝套件。
//
//   node scripts/vapid_keygen.js <私鑰檔案路徑>
//
// - 私鑰寫進你指定的檔案（權限 600、已存在就不覆蓋），畫面上不會印出私鑰。
//   之後把檔案內容貼到 GitHub Environment 的 WEB_PUSH_PRIVATE_KEY，再刪掉這個檔案。
// - 公鑰印在畫面上：它本來就是公開的，設成 GitHub Variables 的 WEB_PUSH_PUBLIC_KEY。
// - 正式專案和 staging 各產生一組，不要共用。
// - 檔名請用 .vapid 結尾（.gitignore 已排除），或放在 repo 以外的資料夾。
"use strict";
const crypto = require("node:crypto");
const fs = require("node:fs");

/** 產生一組金鑰：私鑰是 32 bytes、公鑰是 65 bytes（0x04 + x + y），都用 base64url、不補 = */
function generate() {
  const { privateKey } = crypto.generateKeyPairSync("ec", { namedCurve: "prime256v1" });
  const jwk = privateKey.export({ format: "jwk" });
  const pub = Buffer.concat([
    Buffer.from([4]),
    Buffer.from(jwk.x, "base64url"),
    Buffer.from(jwk.y, "base64url"),
  ]);
  return { privateKey: jwk.d, publicKey: pub.toString("base64url") };
}

function main(argv) {
  const out = argv[0];
  if (!out) {
    console.error("用法：node scripts/vapid_keygen.js <私鑰檔案路徑，例如 ../production.vapid>");
    return 2;
  }
  const keys = generate();
  try {
    fs.writeFileSync(out, keys.privateKey + "\n", { flag: "wx", mode: 0o600 });
  } catch (err) {
    console.error(err.code === "EEXIST" ? `${out} 已經存在，不覆蓋` : `無法寫入 ${out}：${err.message}`);
    return 1;
  }
  console.log(`私鑰已寫入 ${out}（沒有印在畫面上）`);
  console.log(`公鑰（WEB_PUSH_PUBLIC_KEY）：${keys.publicKey}`);
  return 0;
}

if (require.main === module) {
  process.exitCode = main(process.argv.slice(2));
} else {
  module.exports = { generate, main };
}
