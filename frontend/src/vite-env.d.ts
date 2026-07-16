/// <reference types="vite/client" />

/**
 * Typed environment. Only one variable exists, and it is optional by design:
 * the deployed demo runs without it, and its absence must be a normal state the
 * type system understands rather than a crash the reviewer discovers.
 */
interface ImportMetaEnv {
  /** Base URL of a live backend. Unset on the static deploy — see api/client.ts. */
  readonly VITE_API_BASE_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
