import { makePayload, validText } from "./conversation.js";
import { SHARED_MODE } from "./identity.js";

export const LEGACY_TIMEOUT_MS = 35000;
export const CONTOSO_TIMEOUT_MS = 100000;
export const MAX_RESPONSE_BYTES = 262144;
export const NATIVE_CONTOSO_OPTIONS = Object.freeze({ integration: "a2a", studio_variant: "copilot-harness" });

function exact(value, fields) {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    && Object.keys(value).length === fields.length && fields.every(name => Object.hasOwn(value, name));
}

function text(value, max = 512) {
  return typeof value === "string" && value.trim().length > 0 && [...value].length <= max
    && !/[\uD800-\uDFFF]/u.test(value) && !/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/u.test(value);
}

export function validOptions(options) {
  return exact(options, ["integration", "studio_variant"])
    && ["sdk", "a2a"].includes(options.integration)
    && ["standard", "copilot-harness"].includes(options.studio_variant);
}

export function nativeContosoOptions(options) {
  return validOptions(options) && options.integration === NATIVE_CONTOSO_OPTIONS.integration
    && options.studio_variant === NATIVE_CONTOSO_OPTIONS.studio_variant;
}

export function makeContosoPayload(pairs, draft, options, mode = null) {
  if (!validOptions(options) || !validText(draft) || [...draft].length > 2000) {
    throw new Error("Invalid agent question or selection.");
  }
  if (mode !== null && mode !== SHARED_MODE) throw new Error("Invalid identity mode.");
  const extra = { contoso_options: { ...options }, ...(mode === null ? {} : { identity_mode: mode }) };
  const overhead = new TextEncoder().encode(JSON.stringify(extra)).length - 1;
  return { ...makePayload(pairs, draft, 10, 32768 - overhead), ...extra };
}

export function validContoso(value, options) {
  if (!validOptions(options) || !exact(value, [
    "status", "provider_id", "studio_variant", "sources", "citations", "execution_path",
  ]) || !["answered", "insufficient-evidence", "no-matching-capability"].includes(value.status)
    || !Array.isArray(value.sources) || value.sources.length > 32
    || !Array.isArray(value.citations) || value.citations.length > 64
    || !Array.isArray(value.execution_path) || !value.execution_path.length
    || value.execution_path.length > 32 || !value.execution_path.every(item => text(item))) return false;
  if (value.status === "no-matching-capability") {
    return value.provider_id === null && value.studio_variant === null
      && value.sources.length === 0 && value.citations.length === 0;
  }
  if (!text(value.provider_id, 128)
    || (value.studio_variant !== null && value.studio_variant !== options.studio_variant)
    || (value.status === "answered" && value.citations.length === 0)) return false;
  const sources = new Map();
  for (const source of value.sources) {
    if (!exact(source, ["source_id", "provider_id", "version", "title", "classification"])
      || !Object.values(source).every(item => text(item)) || source.provider_id !== value.provider_id
      || sources.has(source.source_id)) return false;
    sources.set(source.source_id, source);
  }
  const cited = new Set();
  for (const citation of value.citations) {
    if (!exact(citation, ["source_id", "version", "section"])
      || !Object.values(citation).every(item => text(item))
      || sources.get(citation.source_id)?.version !== citation.version) return false;
    cited.add(citation.source_id);
  }
  return cited.size === sources.size;
}

export async function readResponse(response) {
  const length = response.headers.get("content-length");
  if (length !== null && (!/^\d+$/.test(length) || Number(length) > MAX_RESPONSE_BYTES)) {
    throw new Error("Invalid response size.");
  }
  const reader = response.body.getReader();
  const chunks = [];
  let size = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > MAX_RESPONSE_BYTES) throw new Error("Invalid response size.");
      chunks.push(value);
    }
  } catch (error) {
    await reader.cancel().catch(() => {});
    throw error;
  } finally {
    reader.releaseLock();
  }
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
  return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
}
