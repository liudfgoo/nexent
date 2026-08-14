import type { StepTokenCount } from "../adapter/remote-chat-model-adapter";

export interface StepContextUsage {
  step: StepTokenCount;
  contextInputTokens: number;
  outputTokens: number;
  contextWindowTokens: number;
  hardBudgetTokens: number | null;
  isOverflow: boolean;
  inputPercent: number;
  outputPercent: number;
  compression: StepCompressionUsage | null;
}

export interface StepCompressionUsage {
  effectiveUncompressedTokens: number;
  postSemanticTokens: number;
  finalTokens: number;
  savedTokens: number;
  savedPercent: number;
  statsComplete: boolean;
  structuralSavedTokens: number;
  structuralCompactCount: number;
  semanticSavedTokens: number | null;
  semanticStatus: string;
  semanticCoveredTurnCount: number | null;
  semanticStatsComplete: boolean;
  summaryGenerationInputTokens: number;
  summaryGenerationOutputTokens: number;
  summaryGenerationCostTokens: number;
  summaryPersistStatus: string | null;
  fallbackCompactionUsed: boolean;
}

export interface ContextOverflow {
  contextInputTokens: number;
  hardBudgetTokens: number;
}

export interface SingleTurnTokenUsageCalculation {
  latest: StepContextUsage;
  previous: StepContextUsage[];
  stepCount: number;
  totalTokensUsed: number;
  usagePercent: number;
  cumulativeSavedTokens: number;
  summaryGenerationCostTokens: number;
}

const normalizeTokenCount = (value: number): number =>
  Number.isFinite(value) && value > 0 ? value : 0;

const normalizeContextWindow = (value: number | null): number | null =>
  value !== null && Number.isFinite(value) && value > 0 ? value : null;

const normalizeOptionalTokenCount = (
  value: number | null | undefined
): number | null =>
  value !== null && value !== undefined && Number.isFinite(value) && value >= 0
    ? value
    : null;

export const calculateStepCompressionUsage = (
  step: StepTokenCount,
  fallbackFinalTokens: number
): StepCompressionUsage | null => {
  const effectiveUncompressedTokens =
    normalizeOptionalTokenCount(step.effectiveUncompressedContextTokens) ??
    normalizeOptionalTokenCount(step.uncompressedEstTokens);
  const explicitFinalTokens = normalizeOptionalTokenCount(
    step.finalContextTokens
  );
  const hasCompressionEvidence =
    effectiveUncompressedTokens !== null ||
    explicitFinalTokens !== null ||
    step.semanticStatus !== null ||
    step.structuralSavedTokens !== null;
  if (!hasCompressionEvidence) return null;

  const finalTokens = explicitFinalTokens ?? fallbackFinalTokens;
  const rawTokens = effectiveUncompressedTokens ?? finalTokens;
  const postSemanticTokens =
    normalizeOptionalTokenCount(step.postSemanticContextTokens) ?? rawTokens;
  const savedTokens =
    normalizeOptionalTokenCount(step.compressionSavedTokens) ??
    Math.max(0, rawTokens - finalTokens);
  const structuralSavedTokens =
    normalizeOptionalTokenCount(step.structuralSavedTokens) ??
    Math.max(0, postSemanticTokens - finalTokens);
  const semanticSavedTokens = normalizeOptionalTokenCount(
    step.semanticSavedTokens
  );
  const summaryGenerationInputTokens =
    normalizeOptionalTokenCount(step.summaryGenerationInputTokens) ?? 0;
  const summaryGenerationOutputTokens =
    normalizeOptionalTokenCount(step.summaryGenerationOutputTokens) ?? 0;

  return {
    effectiveUncompressedTokens: rawTokens,
    postSemanticTokens,
    finalTokens,
    savedTokens,
    savedPercent:
      normalizeOptionalTokenCount(step.compressionRatio) ??
      (rawTokens > 0 ? (savedTokens / rawTokens) * 100 : 0),
    statsComplete: step.compressionStatsComplete ?? true,
    structuralSavedTokens,
    structuralCompactCount:
      normalizeOptionalTokenCount(step.structuralCompactCount) ?? 0,
    semanticSavedTokens,
    semanticStatus: step.semanticStatus ?? "none",
    semanticCoveredTurnCount: normalizeOptionalTokenCount(
      step.semanticCoveredTurnCount
    ),
    semanticStatsComplete: step.semanticStatsComplete ?? true,
    summaryGenerationInputTokens,
    summaryGenerationOutputTokens,
    summaryGenerationCostTokens:
      summaryGenerationInputTokens + summaryGenerationOutputTokens,
    summaryPersistStatus: step.summaryPersistStatus,
    fallbackCompactionUsed: step.fallbackCompactionUsed ?? false,
  };
};

const CONTEXT_OVERFLOW_PATTERN =
  /Context input remains over the model hard budget after compaction:\s*([\d,]+)\s*>\s*([\d,]+)\s*tokens/i;

export const parseContextOverflowError = (
  message: string
): ContextOverflow | null => {
  const match = message.match(CONTEXT_OVERFLOW_PATTERN);
  if (!match) return null;

  const contextInputTokens = Number(match[1].replaceAll(",", ""));
  const hardBudgetTokens = Number(match[2].replaceAll(",", ""));
  if (
    !Number.isFinite(contextInputTokens) ||
    !Number.isFinite(hardBudgetTokens) ||
    contextInputTokens <= hardBudgetTokens ||
    hardBudgetTokens <= 0
  ) {
    return null;
  }

  return { contextInputTokens, hardBudgetTokens };
};

export const calculateStepContextUsage = (
  step: StepTokenCount,
  fallbackContextWindowTokens?: number,
  overflow?: ContextOverflow
): StepContextUsage | null => {
  const contextWindowTokens =
    normalizeContextWindow(step.contextWindowTokens) ??
    (fallbackContextWindowTokens && fallbackContextWindowTokens > 0
      ? fallbackContextWindowTokens
      : null);

  if (contextWindowTokens === null) return null;

  const contextInputTokens = normalizeTokenCount(
    overflow?.contextInputTokens ?? step.estimatedContextTokens
  );
  const outputTokens = normalizeTokenCount(step.stepOutputTokens);
  const hardBudgetTokens =
    overflow?.hardBudgetTokens ??
    normalizeContextWindow(step.hardInputBudgetTokens);
  const isOverflow =
    hardBudgetTokens !== null && contextInputTokens > hardBudgetTokens;
  const inputPercent = Math.min(
    (contextInputTokens / contextWindowTokens) * 100,
    100
  );
  const outputPercent = Math.min(
    (outputTokens / contextWindowTokens) * 100,
    Math.max(0, 100 - inputPercent)
  );
  const compression = calculateStepCompressionUsage(step, contextInputTokens);

  return {
    step,
    contextInputTokens,
    outputTokens,
    contextWindowTokens,
    hardBudgetTokens,
    isOverflow,
    inputPercent,
    outputPercent,
    compression,
  };
};

export const calculateSingleTurnTokenUsage = (
  steps: readonly StepTokenCount[],
  overflow?: ContextOverflow | null
): SingleTurnTokenUsageCalculation | null => {
  if (steps.length === 0) return null;

  const latestStep = steps[steps.length - 1];
  const latestContextWindowTokens =
    normalizeContextWindow(latestStep.contextWindowTokens) ??
    steps
      .toReversed()
      .map((step) => normalizeContextWindow(step.contextWindowTokens))
      .find((value): value is number => value !== null) ??
    null;
  if (latestContextWindowTokens === null) return null;

  const latest = calculateStepContextUsage(
    latestStep,
    latestContextWindowTokens,
    overflow ?? undefined
  );
  if (!latest) return null;

  const previous = steps
    .slice(0, -1)
    .map((step) => calculateStepContextUsage(step, latestContextWindowTokens))
    .filter((step): step is StepContextUsage => step !== null);

  const totalTokensUsed = steps.reduce(
    (sum, step) =>
      sum +
      normalizeTokenCount(step.stepInputTokens) +
      normalizeTokenCount(step.stepOutputTokens),
    0
  );

  return {
    latest,
    previous,
    stepCount: steps.length,
    totalTokensUsed,
    usagePercent: Math.round(
      (latest.contextInputTokens / latest.contextWindowTokens) * 100
    ),
    cumulativeSavedTokens: [latest, ...previous].reduce(
      (sum, stepUsage) => sum + (stepUsage.compression?.savedTokens ?? 0),
      0
    ),
    summaryGenerationCostTokens: [latest, ...previous].reduce(
      (sum, stepUsage) =>
        sum + (stepUsage.compression?.summaryGenerationCostTokens ?? 0),
      0
    ),
  };
};
