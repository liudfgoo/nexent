import assert from "node:assert/strict";
import test from "node:test";

import { buildStreamCustomMetadata } from "./step-token-metadata.ts";

test("returns undefined when no custom stream metadata exists", () => {
  assert.equal(buildStreamCustomMetadata([]), undefined);
});

test("detaches the message snapshot from the mutable run accumulator", () => {
  const runSteps = [{ stepNumber: 1, estimatedContextTokens: 3_200 }];
  const metadata = buildStreamCustomMetadata(runSteps);

  assert.ok(metadata?.stepTokenCounts);
  assert.notEqual(metadata.stepTokenCounts, runSteps);
  assert.notEqual(metadata.stepTokenCounts[0], runSteps[0]);

  runSteps[0].estimatedContextTokens = 9_999;
  runSteps.length = 0;

  assert.deepEqual(metadata.stepTokenCounts, [
    { stepNumber: 1, estimatedContextTokens: 3_200 },
  ]);
});

test("preserves nl2a alongside a detached step snapshot", () => {
  const nl2a = { answer: "draft" };
  const metadata = buildStreamCustomMetadata([{ stepNumber: 2 }], nl2a);

  assert.deepEqual(metadata, {
    stepTokenCounts: [{ stepNumber: 2 }],
    nl2a,
  });
});

test("preserves nl2a when no step token event has arrived yet", () => {
  assert.deepEqual(buildStreamCustomMetadata([], { answer: "draft" }), {
    nl2a: { answer: "draft" },
  });
});
