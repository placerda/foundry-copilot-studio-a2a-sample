const encoder = new TextEncoder();

export function validText(text) {
  return typeof text === "string" && text.trim().length > 0
    && [...text].length <= 4000 && !/[\uD800-\uDFFF]/u.test(text);
}

export function makePayload(pairs, draft, maxMessages = 10, maxBytes = 32768) {
  if (!validText(draft)) throw new Error("Invalid message or message exceeds 4,000 characters.");
  let messages = [{ role: "user", content: draft }];
  const bytes = items => encoder.encode(JSON.stringify({ messages: items })).length;
  if (bytes(messages) > maxBytes) throw new Error("Message exceeds the byte limit.");
  for (let index = pairs.length - 1; index >= 0; index--) {
    const pair = pairs[index];
    if (!validText(pair.user) || !validText(pair.answer)) break;
    const candidate = [{ role: "user", content: pair.user },
      { role: "assistant", content: pair.answer }, ...messages];
    if (candidate.length > maxMessages || bytes(candidate) > maxBytes) break;
    messages = candidate;
  }
  return { messages };
}

export class Conversation {
  constructor() {
    this.generation = 0;
    this.confirmedPairs = [];
    this.pending = null;
    this.draft = "";
  }
  reset() {
    this.generation++;
    this.confirmedPairs = [];
    this.draft = "";
    this.pending?.controller?.abort();
  }
  confirm(generation, user, result) {
    if (generation !== this.generation) return false;
    this.confirmedPairs.push({ user, answer: result.answer, output_limited: result.output_limited });
    this.draft = "";
    return true;
  }
}
