const path = require("node:path");

module.exports = {
  resolve: {
    alias: {
      "@app": path.resolve(__dirname, "src/app"),
      "@ambiguous": path.resolve(__dirname, "src/ambiguous/webpack"),
      "@missing": path.resolve(__dirname, "src/missing"),
    },
  },
};
