import test from "node:test";
import assert from "node:assert/strict";
import { selectedMode, validateCapabilities, validIdentity } from "../identity.js";
import { makeContosoPayload, NATIVE_CONTOSO_OPTIONS } from "../contoso.js";

const capabilities = {
  default: "shared-delegated", modes: {
    "user-delegation": { enabled: false, reason: "OBO unproven." },
    "service-to-service": { enabled: false, reason: "SDK credentials unproven." },
    "shared-delegated": { enabled: true, reason: "Fixed shared connection." },
  },
};

test("backend readiness and allowlisted selection never enable unimplemented modes", () => {
  assert.equal(validateCapabilities(capabilities), capabilities);
  assert.equal(selectedMode("shared-delegated", capabilities), "shared-delegated");
  for (const mode of ["user-delegation", "service-to-service", "attacker", null]) {
    assert.throws(() => selectedMode(mode, capabilities));
  }
  for (const mode of ["user-delegation", "service-to-service"]) {
    const invalid = structuredClone(capabilities);
    invalid.modes[mode].enabled = true;
    assert.throws(() => validateCapabilities(invalid));
    assert.throws(() => selectedMode(mode, invalid));
  }
  assert.throws(() => validateCapabilities({ ...capabilities, connection_id: "attacker" }));
});

test("scenario payload budgets include the allowlisted mode without permitting identities", () => {
  const payload = makeContosoPayload([], "Supplier terms?", NATIVE_CONTOSO_OPTIONS, "shared-delegated");
  assert.equal(payload.identity_mode, "shared-delegated");
  assert.ok(new TextEncoder().encode(JSON.stringify(payload)).length <= 32768);
  assert.throws(() => makeContosoPayload([], "Supplier terms?", NATIVE_CONTOSO_OPTIONS, "attacker"));
  for (const mode of ["user-delegation", "service-to-service"]) {
    assert.throws(() => makeContosoPayload([], "Supplier terms?", NATIVE_CONTOSO_OPTIONS, mode));
  }
});

test("effective Studio identity is not invented from the browser identity", () => {
  const identity = {
    mode: "shared-delegated",
    application_caller: { tenant_id: "11111111-1111-1111-1111-111111111111",
      object_id: "22222222-2222-2222-2222-222222222222" },
    studio_caller: { kind: "fixed-shared-user", verified: false },
  };
  assert.ok(validIdentity(identity, "shared-delegated"));
  assert.ok(!validIdentity(identity, "service-to-service"));
  assert.ok(!validIdentity({ ...identity, studio_caller: { kind: "fixed-shared-user", verified: true } }, "shared-delegated"));
});
