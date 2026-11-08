import test from "node:test";
import assert from "node:assert/strict";
import { Conversation, makePayload } from "../conversation.js";
import { makeContosoPayload, validContoso, validOptions, readResponse,
  CONTOSO_TIMEOUT_MS, LEGACY_TIMEOUT_MS, MAX_RESPONSE_BYTES, nativeContosoOptions, NATIVE_CONTOSO_OPTIONS } from "../contoso.js";

const pair = (user = "u", answer = "a") => ({ user, answer, output_limited: true });

test("default maximum includes newest four full pairs and current user", () => {
  const pairs = Array.from({ length: 6 }, (_, i) => pair(`u${i}`, `a${i}`));
  const payload = makePayload(pairs, "now");
  assert.equal(payload.messages.length, 9);
  assert.equal(payload.messages[0].content, "u2");
  assert.deepEqual(payload.messages.at(-1), { role: "user", content: "now" });
  assert.ok(!JSON.stringify(payload).includes("output_limited"));
});

test("Unicode and invalid draft boundaries", () => {
  assert.equal(makePayload([], "😀".repeat(4000)).messages[0].content.length, 8000);
  for (const draft of ["", "  ", "x".repeat(4001), "\ud800"]) {
    assert.throws(() => makePayload([], draft));
  }
});

test("byte budget counts actual serialized UTF-8 including escaped content", () => {
  const pairs = Array.from({ length: 4 }, () => pair("😀".repeat(2000), "\\".repeat(4000)));
  const payload = makePayload(pairs, "now");
  assert.ok(new TextEncoder().encode(JSON.stringify(payload)).length <= 32768);
  assert.equal(payload.messages.length, 5);
  assert.ok(new TextEncoder().encode(JSON.stringify({
    messages: [{ role: "user", content: pairs[0].user },
      { role: "assistant", content: pairs[0].answer }, ...payload.messages],
  })).length > 32768);
  const exact = makePayload([], "x", 10, 44);
  assert.equal(new TextEncoder().encode(JSON.stringify(exact)).length, 44);
  assert.throws(() => makePayload([], "x", 10, 43));
});

test("oversized answer is a barrier, displayed text is not truncated", () => {
  const pairs = [pair("old"), pair("big", "a".repeat(4001)), pair("recent")];
  assert.deepEqual(makePayload(pairs, "now").messages.map(message => message.content), ["recent", "a", "now"]);
  assert.equal(pairs[1].answer.length, 4001);
});

test("generation invalidates old confirmations and reset does not release pending lock", () => {
  const conversation = new Conversation();
  conversation.pending = { operation: 1 };
  const generation = conversation.generation;
  conversation.reset();
  assert.equal(conversation.pending.operation, 1);
  assert.equal(conversation.confirm(generation, "old", { answer: "late", output_limited: true }), false);
  assert.equal(conversation.confirm(conversation.generation, "now", { answer: "ok", output_limited: false }), true);
  assert.equal(conversation.confirmedPairs.length, 1);
});

const contosoResult = (variant = "standard") => ({
  status: "answered", provider_id: "actual-provider", studio_variant: variant,
  sources: [{ source_id: "actual-source", provider_id: "actual-provider", version: "1",
    title: "Actual synthetic source", classification: "Synthetic demonstration data" }],
  citations: [{ source_id: "actual-source", version: "1", section: "Evidence limits" }],
  execution_path: ["Actual gateway", "Actual provider"],
});

test("Contoso request selections have exact payload and separate latency budget", () => {
  assert.equal(CONTOSO_TIMEOUT_MS, 100000);
  assert.equal(LEGACY_TIMEOUT_MS, 35000);
  for (const integration of ["sdk", "a2a"]) {
    for (const studio_variant of ["standard", "copilot-harness"]) {
      const options = { integration, studio_variant };
      const payload = makeContosoPayload([pair("old", "previous")], "same question", options);
      assert.deepEqual(payload, { messages: [
        { role: "user", content: "old" }, { role: "assistant", content: "previous" },
        { role: "user", content: "same question" }], contoso_options: options });
      assert.ok(validContoso(contosoResult(studio_variant), options));
      assert.notEqual(payload.contoso_options, options);
      assert.ok(new TextEncoder().encode(JSON.stringify(payload)).length <= 32768);
    }
  }
  const pairs = Array.from({ length: 5 }, () => pair("😀".repeat(2000), "\\".repeat(4000)));
  assert.ok(new TextEncoder().encode(JSON.stringify(makeContosoPayload(pairs, "question",
    { integration: "a2a", studio_variant: "copilot-harness" }))).length <= 32768);
});

test("Contoso rejects malformed options, identity fields and over-limit questions", () => {
  for (const options of [null, {}, "sdk", { integration: "sdk" },
    { integration: "a2a", cia_variant: "copilot-harness" },
    { integration: "sdk", studio_variant: "other" },
    { integration: "sdk", studio_variant: "standard", gateway_url: "https://evil.com" }]) {
    assert.equal(validOptions(options), false);
    assert.throws(() => makeContosoPayload([], "q", options));
  }
  assert.throws(() => makeContosoPayload([], "x".repeat(2001),
    { integration: "sdk", studio_variant: "standard" }));
  assert.doesNotThrow(() => makeContosoPayload([], "😀".repeat(2000),
    { integration: "sdk", studio_variant: "standard" }));
});

test("Contoso validates actual attribution, rejects success-shaped fallback and variant substitution", () => {
  const options = { integration: "sdk", studio_variant: "standard" };
  for (const changes of [
    { status: "unavailable" }, { provider_id: null }, { studio_variant: "copilot-harness" },
    { sources: [] }, { citations: [] }, { execution_path: [] }, { execution_path: ["\ud800"] },
    { caller_id: "secret" }, { sources: [{ source_id: "actual-source" }] },
    { citations: [{ source_id: "fake", version: "1", section: "x" }] },
    { citations: [{ source_id: "actual-source", version: "wrong", section: "x" }] },
  ]) assert.equal(validContoso({ ...contosoResult(), ...changes }, options), false);
  const wrongProvider = contosoResult();
  wrongProvider.sources[0].provider_id = "other";
  assert.equal(validContoso(wrongProvider, options), false);
  const duplicate = contosoResult();
  duplicate.sources.push({ ...duplicate.sources[0] });
  assert.equal(validContoso(duplicate, options), false);
  assert.equal(validContoso(contosoResult(null), options), true);
  const noMatch = { ...contosoResult(), status: "no-matching-capability", provider_id: null,
    studio_variant: null, sources: [], citations: [], execution_path: ["No matching capability"] };
  assert.equal(validContoso(noMatch, options), true);
  assert.equal(validContoso({ ...noMatch, sources: contosoResult().sources }, options), false);
  assert.equal(validContoso({ ...contosoResult(), status: "insufficient-evidence", sources: [], citations: [] }, options), true);
});

test("Contoso response reader bounds bytes and rejects invalid UTF-8/JSON", async () => {
  assert.deepEqual(await readResponse(new Response(JSON.stringify({ result: true }))), { result: true });
  await assert.rejects(readResponse(new Response("small",
    { headers: { "Content-Length": String(MAX_RESPONSE_BYTES + 1) } })));
  await assert.rejects(readResponse(new Response("x".repeat(MAX_RESPONSE_BYTES + 1))));
  await assert.rejects(readResponse(new Response(new Uint8Array([255]))));
  await assert.rejects(readResponse(new Response("not JSON")));
});

test("active native route is fixed to A2A / Copilot Harness without deleting diagnostic helpers", () => {
  assert.deepEqual(NATIVE_CONTOSO_OPTIONS, { integration: "a2a", studio_variant: "copilot-harness" });
  assert.equal(nativeContosoOptions(NATIVE_CONTOSO_OPTIONS), true);
  assert.equal(nativeContosoOptions({ integration: "sdk", studio_variant: "copilot-harness" }), false);
  assert.equal(nativeContosoOptions({ integration: "a2a", studio_variant: "standard" }), false);
  assert.equal(nativeContosoOptions({ ...NATIVE_CONTOSO_OPTIONS, gateway_url: "untrusted" }), false);
});
