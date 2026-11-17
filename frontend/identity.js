export const MODES = ["user-delegation", "service-to-service", "shared-delegated"];
export const SHARED_MODE = "shared-delegated";

export function validateCapabilities(value) {
  if (!value || Object.keys(value).sort().join() !== "default,modes"
      || value.default !== SHARED_MODE
      || !value.modes || Object.keys(value.modes).sort().join() !== [...MODES].sort().join()) {
    throw new Error("Invalid identity readiness.");
  }
  for (const mode of MODES) {
    const entry = value.modes[mode];
    if (!entry || Object.keys(entry).sort().join() !== "enabled,reason"
        || typeof entry.enabled !== "boolean" || typeof entry.reason !== "string"
        || !entry.reason.trim() || entry.reason.length > 1000) {
      throw new Error("Invalid identity readiness.");
    }
  }
  // These adapters and the integrity-protected user context are not implemented.
  if (value.modes["user-delegation"].enabled || value.modes["service-to-service"].enabled) {
    throw new Error("Unimplemented identity adapter.");
  }
  return value;
}

export function selectedMode(mode, capabilities) {
  if (mode !== SHARED_MODE || capabilities?.modes[mode]?.enabled !== true) {
    throw new Error("This identity scenario is unavailable. No fallback was attempted.");
  }
  return mode;
}

export function validIdentity(value, mode) {
  const guid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  return value && Object.keys(value).sort().join() === "application_caller,mode,studio_caller"
    && value.mode === mode && value.application_caller
    && Object.keys(value.application_caller).sort().join() === "object_id,tenant_id"
    && guid.test(value.application_caller.tenant_id) && guid.test(value.application_caller.object_id)
    && value.studio_caller && Object.keys(value.studio_caller).sort().join() === "kind,verified"
    && value.studio_caller.kind === "fixed-shared-user" && value.studio_caller.verified === false;
}
