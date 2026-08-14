import assert from "node:assert/strict";
import test from "node:test";

import {
  calculateSingleTurnTokenUsage,
  calculateStepContextUsage,
  parseContextOverflowError,
} from "./token-usage-calculation.ts";

const makeStep = (overrides = {}) => ({
  stepNumber: 1,
  duration: 1,
  stepInputTokens: 1_000,
  stepOutputTokens: 100,
  totalOutputTokens: 100,
  estimatedContextTokens: 1_200,
  tokenThreshold: 7_000,
  contextWindowTokens: 8_000,
  hardInputBudgetTokens: 7_000,
  contextProcessingMode: "adaptive_compact",
  compressionCalls: null,
  compressionInputTokens: null,
  compressionOutputTokens: null,
  compressionRatio: null,
  uncompressedEstTokens: null,
  effectiveUncompressedContextTokens: null,
  postSemanticContextTokens: null,
  finalContextTokens: null,
  compressionSavedTokens: null,
  compressionStatsComplete: null,
  structuralSavedTokens: null,
  structuralCompactCount: null,
  semanticSavedTokens: null,
  semanticStatus: null,
  semanticCoveredTurnCount: null,
  semanticStatsComplete: null,
  summaryGenerationInputTokens: null,
  summaryGenerationOutputTokens: null,
  summaryPersistStatus: null,
  fallbackCompactionUsed: null,
  ...overrides,
});

test("uses the latest estimated context for the headline ratio", () => {
  const result = calculateSingleTurnTokenUsage([
    makeStep(),
    makeStep({
      stepNumber: 2,
      stepInputTokens: 3_500,
      stepOutputTokens: 200,
      estimatedContextTokens: 3_200,
    }),
  ]);

  assert.ok(result);
  assert.equal(result.usagePercent, 40);
  assert.equal(result.latest.contextInputTokens, 3_200);
  assert.equal(result.latest.contextWindowTokens, 8_000);
  assert.equal(result.totalTokensUsed, 4_800);
  assert.equal(result.previous.length, 1);
  assert.equal(result.previous[0].inputPercent, 15);
  assert.equal(result.previous[0].outputPercent, 1.25);
});

test("uses each previous step context window and falls back to the latest window", () => {
  const result = calculateSingleTurnTokenUsage([
    makeStep({ contextWindowTokens: 4_000 }),
    makeStep({ stepNumber: 2, contextWindowTokens: null }),
    makeStep({ stepNumber: 3, contextWindowTokens: 8_000 }),
  ]);

  assert.ok(result);
  assert.equal(result.previous[0].contextWindowTokens, 4_000);
  assert.equal(result.previous[1].contextWindowTokens, 8_000);
});

test("clamps progress widths and sanitizes invalid token values", () => {
  const result = calculateStepContextUsage(
    makeStep({
      estimatedContextTokens: 9_000,
      stepOutputTokens: Number.NaN,
    })
  );

  assert.ok(result);
  assert.equal(result.inputPercent, 100);
  assert.equal(result.outputPercent, 0);
  assert.equal(result.outputTokens, 0);
});

test("treats the streamed hard budget as an advisory overflow", () => {
  const result = calculateStepContextUsage(
    makeStep({
      estimatedContextTokens: 7_500,
      hardInputBudgetTokens: 7_000,
      contextWindowTokens: 8_000,
    })
  );

  assert.ok(result);
  assert.equal(result.hardBudgetTokens, 7_000);
  assert.equal(result.isOverflow, true);
  assert.equal(result.inputPercent, 93.75);
});

test("returns null without steps or a valid latest context window", () => {
  assert.equal(calculateSingleTurnTokenUsage([]), null);
  assert.equal(
    calculateSingleTurnTokenUsage([makeStep({ contextWindowTokens: null })]),
    null
  );
  assert.equal(
    calculateStepContextUsage(makeStep({ contextWindowTokens: 0 })),
    null
  );
});

test("uses the failed step overflow estimate instead of its zero token count", () => {
  const overflow = parseContextOverflowError(
    "Run Agent Error: Error in interaction: Context input remains over the model hard budget after compaction: 24133 > 21299 tokens"
  );
  const result = calculateSingleTurnTokenUsage(
    [
      makeStep({ stepInputTokens: 3_157, stepOutputTokens: 1_107 }),
      makeStep({
        stepNumber: 2,
        stepInputTokens: 4_394,
        stepOutputTokens: 3_047,
      }),
      makeStep({
        stepNumber: 3,
        stepInputTokens: 10_579,
        stepOutputTokens: 5_991,
      }),
      makeStep({
        stepNumber: 4,
        stepInputTokens: 12_977,
        stepOutputTokens: 6_569,
      }),
      makeStep({
        stepNumber: 5,
        stepInputTokens: 0,
        stepOutputTokens: 0,
        estimatedContextTokens: 0,
        contextWindowTokens: 32_768,
      }),
    ],
    overflow
  );

  assert.deepEqual(overflow, {
    contextInputTokens: 24_133,
    hardBudgetTokens: 21_299,
  });
  assert.ok(result);
  assert.equal(result.stepCount, 5);
  assert.equal(result.latest.contextInputTokens, 24_133);
  assert.equal(result.latest.contextWindowTokens, 32_768);
  assert.equal(result.latest.hardBudgetTokens, 21_299);
  assert.equal(result.latest.isOverflow, true);
  assert.equal(result.usagePercent, 74);
  assert.equal(result.totalTokensUsed, 47_821);
  assert.equal(result.previous.length, 4);
});

test("ignores unrelated or invalid context errors", () => {
  assert.equal(
    parseContextOverflowError("Run Agent Error: network error"),
    null
  );
  assert.equal(
    parseContextOverflowError(
      "Context input remains over the model hard budget after compaction: 20000 > 21299 tokens"
    ),
    null
  );
});

test("calculates deterministic structural compression without summary calls", () => {
  const result = calculateSingleTurnTokenUsage([
    makeStep({
      effectiveUncompressedContextTokens: 10_000,
      postSemanticContextTokens: 10_000,
      finalContextTokens: 7_500,
      estimatedContextTokens: 7_500,
      compressionSavedTokens: 2_500,
      compressionStatsComplete: true,
      structuralSavedTokens: 2_500,
      structuralCompactCount: 3,
      semanticSavedTokens: 0,
      semanticStatus: "none",
      semanticStatsComplete: true,
      compressionCalls: 0,
    }),
  ]);

  assert.ok(result);
  assert.equal(result.latest.compression.savedTokens, 2_500);
  assert.equal(result.latest.compression.savedPercent, 25);
  assert.equal(result.latest.compression.structuralSavedTokens, 2_500);
  assert.equal(result.latest.compression.semanticSavedTokens, 0);
  assert.equal(result.cumulativeSavedTokens, 2_500);
});

test("keeps semantic savings and summary generation cost separate", () => {
  const result = calculateSingleTurnTokenUsage([
    makeStep({
      effectiveUncompressedContextTokens: 12_000,
      postSemanticContextTokens: 8_000,
      finalContextTokens: 7_000,
      estimatedContextTokens: 7_000,
      compressionSavedTokens: 5_000,
      structuralSavedTokens: 1_000,
      structuralCompactCount: 1,
      semanticSavedTokens: 4_000,
      semanticStatus: "updated",
      semanticCoveredTurnCount: 8,
      semanticStatsComplete: true,
      compressionStatsComplete: true,
      summaryGenerationInputTokens: 900,
      summaryGenerationOutputTokens: 220,
    }),
    makeStep({
      stepNumber: 2,
      effectiveUncompressedContextTokens: 13_000,
      postSemanticContextTokens: 9_000,
      finalContextTokens: 8_500,
      estimatedContextTokens: 8_500,
      compressionSavedTokens: 4_500,
      structuralSavedTokens: 500,
      semanticSavedTokens: 4_000,
      semanticStatus: "reused",
      semanticCoveredTurnCount: 8,
      semanticStatsComplete: true,
      compressionStatsComplete: true,
      summaryGenerationInputTokens: 0,
      summaryGenerationOutputTokens: 0,
    }),
  ]);

  assert.ok(result);
  assert.equal(result.cumulativeSavedTokens, 9_500);
  assert.equal(result.summaryGenerationCostTokens, 1_120);
  assert.equal(result.latest.compression.semanticStatus, "reused");
  assert.equal(result.latest.compression.semanticSavedTokens, 4_000);
});

test("does not fabricate semantic savings for a legacy checkpoint", () => {
  const result = calculateSingleTurnTokenUsage([
    makeStep({
      effectiveUncompressedContextTokens: 8_000,
      postSemanticContextTokens: 8_000,
      finalContextTokens: 7_500,
      compressionSavedTokens: 500,
      structuralSavedTokens: 500,
      semanticSavedTokens: null,
      semanticStatus: "reused",
      semanticStatsComplete: false,
      compressionStatsComplete: false,
    }),
  ]);

  assert.ok(result);
  assert.equal(result.latest.compression.semanticSavedTokens, null);
  assert.equal(result.latest.compression.semanticStatsComplete, false);
  assert.equal(result.latest.compression.statsComplete, false);
});

test("restores overall compression from legacy token_count fields", () => {
  const result = calculateSingleTurnTokenUsage([
    makeStep({
      estimatedContextTokens: 2_882,
      uncompressedEstTokens: 4_630,
      compressionRatio: 37.8,
    }),
  ]);

  assert.ok(result);
  assert.equal(result.latest.compression.effectiveUncompressedTokens, 4_630);
  assert.equal(result.latest.compression.finalTokens, 2_882);
  assert.equal(result.latest.compression.savedTokens, 1_748);
  assert.equal(result.latest.compression.savedPercent, 37.8);
  assert.equal(result.latest.compression.semanticStatus, "none");
});
