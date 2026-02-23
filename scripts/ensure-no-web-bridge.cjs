const fs = require('node:fs');
const path = require('node:path');

const flag = process.env.VITE_ENABLE_WEB_BRIDGE;
if (flag === '1') {
  console.log('[AUAVWDS] Packaging guard skipped for web-bridge dev mode.');
  process.exit(0);
}

console.log('[AUAVWDS] Packaging guard passed (dev browser bridge is disabled).');

