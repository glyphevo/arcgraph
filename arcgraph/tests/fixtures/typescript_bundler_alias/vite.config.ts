import { fileURLToPath } from "node:url";

export default {
  resolve: {
    alias: [
      { find: "@", replacement: fileURLToPath(new URL("./src", import.meta.url)) },
      { find: "@config", replacement: "./src/config.ts" },
      { find: "@ambiguous", replacement: "./src/ambiguous/vite" },
      { find: "@dynamic", replacement: process.env.DYNAMIC_ALIAS },
      { find: /^@regex\/(.*)/, replacement: "./src/regex/$1" },
    ],
  },
};
