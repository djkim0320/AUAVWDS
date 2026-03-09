/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_ENABLE_WEB_BRIDGE?: string;
  readonly VITE_BACKEND_PROXY_TARGET?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
