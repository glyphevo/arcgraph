export function readEnvConfig() {
  const apiBase = process.env.API_BASE_URL;
  const publicToken = process.env["PUBLIC_TOKEN"];
  const viteFlag = import.meta.env.VITE_PUBLIC_FLAG;
  const viteMode = import.meta.env["VITE_MODE"];
  return `${apiBase}:${publicToken}:${viteFlag}:${viteMode}`;
}

export function useBrowserStorage() {
  const theme = localStorage.getItem("theme");
  const globalTheme = window.localStorage.getItem("global-theme");
  sessionStorage.setItem("session-id", "abc");
  globalThis.sessionStorage.setItem("global-session", "xyz");
  localStorage.removeItem("legacy-theme");
  return `${theme}:${globalTheme}`;
}

export function dynamicConfig(envKey: string, storageKey: string) {
  const value = process.env[envKey];
  const viteValue = import.meta.env[envKey];
  const cached = localStorage.getItem(storageKey);
  const sessionCached = globalThis.sessionStorage.getItem(storageKey);
  return value || viteValue || cached || sessionCached;
}
