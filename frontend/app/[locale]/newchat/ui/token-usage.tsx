"use client";

import { useRef, useState, type FC } from "react";
import { useTranslation } from "react-i18next";
import { useAuiState, useMessageTiming } from "@assistant-ui/react";
import { Zap } from "lucide-react";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { type StepTokenCount } from "../adapter/remote-chat-model-adapter";
import {
  calculateSingleTurnTokenUsage,
  parseContextOverflowError,
} from "./token-usage-calculation";

interface TokenUsageProps {
  className?: string;
}

/**
 * Displays conversation-level token usage (for future use).
 * Currently not implemented - reserved for total conversation token tracking.
 */
export const TokenUsage: FC<TokenUsageProps> = ({ className }) => {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(false);
  const timing = useMessageTiming();

  if (!timing?.tokenCount) return null;

  const tokenCount = timing.tokenCount;
  const usagePercent = Math.round((tokenCount / 128000) * 100);

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setExpanded(!expanded)}
        className={`flex items-center gap-1.5 rounded-md px-2 py-1 text-xs text-muted-foreground transition-colors hover:bg-muted ${className ?? ""}`}
      >
        <Zap className="size-3 text-amber-500" />
        <span className="font-medium text-foreground">{usagePercent}%</span>
        <span className="text-muted-foreground/70">
          {t("chat.tokenUsage.used")}
        </span>
      </button>

      {/* Expanded details popover */}
      {expanded && (
        <div className="absolute bottom-full right-0 z-50 mb-1 w-64 rounded-lg border border-border bg-popover p-3 shadow-lg">
          <div className="mb-3 flex items-center justify-between">
            <span className="text-xs font-medium text-foreground">
              {t("chat.tokenUsage.details")}
            </span>
            <button
              type="button"
              onClick={() => setExpanded(false)}
              className="text-muted-foreground hover:text-foreground"
            >
              <span className="sr-only">{t("chat.tokenUsage.close")}</span>
              <svg
                className="size-3.5"
                fill="none"
                viewBox="0 0 24 24"
                stroke="currentColor"
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeWidth={2}
                  d="M6 18L18 6M6 6l12 12"
                />
              </svg>
            </button>
          </div>

          {/* Progress bar */}
          <div className="mb-3">
            <div className="mb-1 flex justify-between text-xs">
              <span className="text-muted-foreground">
                {t("chat.tokenUsage.context")}
              </span>
              <span className="font-medium text-foreground">
                {tokenCount.toLocaleString()} / 128000
              </span>
            </div>
            <div className="h-2 overflow-hidden rounded-full bg-muted">
              <div
                className="h-full rounded-full bg-amber-500 transition-all"
                style={{ width: `${Math.min(usagePercent, 100)}%` }}
              />
            </div>
          </div>

          {/* Details */}
          <div className="space-y-2 text-xs">
            <div className="flex items-center justify-between">
              <span className="flex items-center gap-1.5 text-muted-foreground">
                <span className="size-2 rounded-full bg-blue-500" />
                {t("chat.tokenUsage.output")}
              </span>
              <span className="font-medium text-foreground">
                {tokenCount.toLocaleString()}
              </span>
            </div>
            {timing.tokensPerSecond !== undefined && (
              <div className="flex items-center justify-between">
                <span className="flex items-center gap-1.5 text-muted-foreground">
                  <span className="size-2 rounded-full bg-green-500" />
                  {t("chat.tokenUsage.speed")}
                </span>
                <span className="font-medium text-foreground">
                  {timing.tokensPerSecond.toFixed(1)} tok/s
                </span>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
};

TokenUsage.displayName = "TokenUsage";

// ============================================================
// SingleTurnTokenUsage - Per-turn step-by-step token display
// ============================================================

interface SingleTurnTokenUsageProps {
  className?: string;
}

/**
 * Displays the turn's cumulative actual token usage relative to the model's
 * context window. Earlier steps remain available from the step-count tooltip.
 *
 * Data source resolution:
 * Both live runs and historical restores bind a detached step-token snapshot to
 * `metadata.custom.stepTokenCounts`, so every rendered message owns its data.
 */
export const SingleTurnTokenUsage: FC<SingleTurnTokenUsageProps> = ({
  className,
}) => {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(false);
  const [stepHistoryOpen, setStepHistoryOpen] = useState(false);
  const [stepHistoryPinned, setStepHistoryPinned] = useState(false);
  const stepHistoryCloseTimer = useRef<ReturnType<typeof setTimeout> | null>(
    null
  );

  const messageSteps = useAuiState((s) => {
    const custom = s.message.metadata?.custom as
      | { stepTokenCounts?: StepTokenCount[] }
      | undefined;
    return custom?.stepTokenCounts;
  });
  const messageContent = useAuiState(
    (s) => s.message.content
  ) as ReadonlyArray<{
    type?: string;
    text?: string;
  }>;

  const steps: readonly StepTokenCount[] = messageSteps ?? [];
  const contextOverflow = parseContextOverflowError(
    messageContent
      .filter((part) => part.type === "text" && typeof part.text === "string")
      .map((part) => part.text)
      .join("\n")
  );

  const usage = calculateSingleTurnTokenUsage(steps, contextOverflow);
  if (!usage) return null;

  const {
    latest,
    previous,
    stepCount,
    totalInputTokens,
    totalOutputTokens,
    totalTokensUsed,
    totalInputPercent,
    totalOutputPercent,
    usagePercent,
    cumulativeSavedTokens,
    summaryGenerationCostTokens,
  } = usage;

  const formatCompactTokens = (tokens: number) => {
    if (tokens < 1_000) return tokens.toLocaleString();
    const compact = tokens / 1_000;
    return `${compact >= 10 ? compact.toFixed(0) : compact.toFixed(1)}k`;
  };

  const semanticStatusLabel = (status: string) => {
    switch (status) {
      case "created":
        return t("chat.tokenUsage.semanticStatusCreated");
      case "updated":
        return t("chat.tokenUsage.semanticStatusUpdated");
      case "reused":
        return t("chat.tokenUsage.semanticStatusReused");
      case "failed":
        return t("chat.tokenUsage.semanticStatusFailed");
      default:
        return t("chat.tokenUsage.semanticStatusNone");
    }
  };

  const stepSummary = (stepUsage: typeof latest) =>
    t("chat.tokenUsage.stepSummary", {
      step: stepUsage.step.stepNumber,
      input: stepUsage.contextInputTokens.toLocaleString(),
      output: stepUsage.outputTokens.toLocaleString(),
    });

  const cancelStepHistoryClose = () => {
    if (stepHistoryCloseTimer.current !== null) {
      clearTimeout(stepHistoryCloseTimer.current);
      stepHistoryCloseTimer.current = null;
    }
  };

  const openStepHistoryPreview = () => {
    cancelStepHistoryClose();
    setStepHistoryOpen(true);
  };

  const scheduleStepHistoryClose = () => {
    cancelStepHistoryClose();
    stepHistoryCloseTimer.current = setTimeout(() => {
      if (!stepHistoryPinned) setStepHistoryOpen(false);
    }, 120);
  };

  const toggleStepHistoryPinned = (
    event: React.MouseEvent<HTMLButtonElement>
  ) => {
    event.preventDefault();
    cancelStepHistoryClose();
    const nextPinned = !stepHistoryPinned;
    setStepHistoryPinned(nextPinned);
    setStepHistoryOpen(nextPinned);
  };

  const renderStepProgress = (stepUsage: typeof latest) => (
    <div
      className="flex h-3 overflow-hidden rounded-full bg-muted"
      title={stepSummary(stepUsage)}
    >
      <div
        className="h-full bg-blue-500"
        style={{ width: `${stepUsage.inputPercent}%` }}
      />
      <div
        className="h-full bg-amber-500"
        style={{ width: `${stepUsage.outputPercent}%` }}
      />
    </div>
  );

  const renderTotalProgress = () => (
    <div
      className="flex h-3 overflow-hidden rounded-full bg-muted"
      title={t("chat.tokenUsage.actualTokenBreakdown", {
        input: totalInputTokens.toLocaleString(),
        output: totalOutputTokens.toLocaleString(),
      })}
    >
      <div
        className="h-full bg-blue-500"
        style={{ width: `${totalInputPercent}%` }}
      />
      <div
        className="h-full bg-amber-500"
        style={{ width: `${totalOutputPercent}%` }}
      />
    </div>
  );

  const renderStepCompression = (stepUsage: typeof latest, compact = false) => {
    const compression = stepUsage.compression;
    if (!compression) return null;
    const hasSavings = compression.savedTokens > 0;
    const hasCompressionDetail =
      hasSavings || compression.semanticStatus !== "none";
    const isDisabled = stepUsage.step.contextProcessingMode === "passthrough";

    if (compact) {
      return (
        <div className="mt-1.5 text-[11px] text-muted-foreground">
          {hasSavings ? (
            <>
              {compression.effectiveUncompressedTokens.toLocaleString()} →{" "}
              {compression.finalTokens.toLocaleString()} ·{" "}
              <span className="text-emerald-600 dark:text-emerald-400">
                {t("chat.tokenUsage.savedTokens", {
                  tokens: compression.savedTokens.toLocaleString(),
                  percent: compression.savedPercent.toFixed(1),
                })}
              </span>
            </>
          ) : compression.semanticStatus !== "none" ? (
            <span>
              {t("chat.tokenUsage.historySemanticCompression")} ·{" "}
              {semanticStatusLabel(compression.semanticStatus)}
            </span>
          ) : (
            <span>
              {isDisabled
                ? t("chat.tokenUsage.compressionNotEnabled")
                : t("chat.tokenUsage.compressionNotTriggered")}
            </span>
          )}
        </div>
      );
    }

    return (
      <div className="mb-3 border-t border-border pt-3 text-xs">
        <div className="mb-2 flex items-center justify-between gap-3">
          <span className="font-medium text-foreground">
            {t("chat.tokenUsage.compressionEffect")}
          </span>
          {hasSavings && (
            <span className="font-medium text-emerald-600 dark:text-emerald-400">
              {t("chat.tokenUsage.savedTokens", {
                tokens: compression.savedTokens.toLocaleString(),
                percent: compression.savedPercent.toFixed(1),
              })}
            </span>
          )}
        </div>

        {hasCompressionDetail ? (
          <div className="rounded-md bg-muted/60 p-2.5">
            {hasSavings && (
              <div className="mb-2 text-muted-foreground">
                {t("chat.tokenUsage.compressionRange", {
                  before:
                    compression.effectiveUncompressedTokens.toLocaleString(),
                  after: compression.finalTokens.toLocaleString(),
                })}
                {!compression.statsComplete && (
                  <span className="ml-1">
                    · {t("chat.tokenUsage.statsIncomplete")}
                  </span>
                )}
              </div>
            )}

            {(compression.structuralSavedTokens > 0 ||
              compression.structuralCompactCount > 0 ||
              compression.fallbackCompactionUsed) && (
              <div className="flex items-center justify-between gap-3 border-t border-border/60 py-2">
                <span className="flex items-center gap-1.5 text-foreground">
                  <span className="size-2 rounded-sm bg-violet-500" />
                  {t("chat.tokenUsage.structuralCompression")}
                </span>
                <span className="text-right text-muted-foreground">
                  <span className="block font-medium text-foreground">
                    {compression.structuralSavedTokens.toLocaleString()} Token
                  </span>
                  {t("chat.tokenUsage.compactedItems", {
                    count: compression.structuralCompactCount,
                  })}
                </span>
              </div>
            )}

            {compression.semanticStatus !== "none" && (
              <div className="flex items-center justify-between gap-3 border-t border-border/60 pt-2">
                <span className="flex items-center gap-1.5 text-foreground">
                  <span className="size-2 rounded-sm bg-teal-500" />
                  {t("chat.tokenUsage.historySemanticCompression")}
                </span>
                <span className="text-right text-muted-foreground">
                  <span className="block font-medium text-foreground">
                    {compression.semanticStatsComplete &&
                    compression.semanticSavedTokens !== null
                      ? `${compression.semanticSavedTokens.toLocaleString()} Token`
                      : t("chat.tokenUsage.statsUnavailable")}
                  </span>
                  {compression.semanticCoveredTurnCount !== null &&
                    `${t("chat.tokenUsage.coveredTurns", {
                      count: compression.semanticCoveredTurnCount,
                    })} · `}
                  {semanticStatusLabel(compression.semanticStatus)}
                </span>
              </div>
            )}
          </div>
        ) : (
          <div className="rounded-md bg-muted/60 px-2.5 py-2 text-muted-foreground">
            {isDisabled
              ? t("chat.tokenUsage.compressionNotEnabled")
              : t("chat.tokenUsage.compressionNotTriggered")}
          </div>
        )}
      </div>
    );
  };

  return (
    <Popover
      open={expanded}
      onOpenChange={(open) => {
        setExpanded(open);
        if (!open) {
          cancelStepHistoryClose();
          setStepHistoryOpen(false);
          setStepHistoryPinned(false);
        }
      }}
    >
      <PopoverTrigger asChild>
        <button
          type="button"
          className={`flex items-center gap-1.5 rounded-md px-2 py-1 text-xs text-muted-foreground transition-colors hover:bg-muted ${className ?? ""}`}
        >
          <Zap className="size-3 text-amber-500" />
          <span
            className={`font-medium ${latest.isOverflow ? "text-amber-600 dark:text-amber-400" : "text-foreground"}`}
          >
            {latest.isOverflow
              ? t("chat.tokenUsage.overflow")
              : `${usagePercent}%`}
          </span>
          <span className="text-muted-foreground/70">
            {t("chat.tokenUsage.turn")}
          </span>
          {latest.compression && latest.compression.savedTokens > 0 && (
            <span className="text-emerald-600 dark:text-emerald-400">
              ·{" "}
              {t("chat.tokenUsage.savedCompact", {
                tokens: formatCompactTokens(latest.compression.savedTokens),
              })}
            </span>
          )}
        </button>
      </PopoverTrigger>

      {/* Expanded details popover */}
      <PopoverContent
        side="top"
        align="end"
        collisionPadding={12}
        className="max-h-[calc(100vh-1.5rem)] w-80 overflow-y-auto p-3"
      >
        <div className="mb-3 flex items-center justify-between">
          <span className="text-xs font-medium text-foreground">
            {t("chat.tokenUsage.turnDetails")}
          </span>
          <button
            type="button"
            onClick={() => setExpanded(false)}
            className="text-muted-foreground hover:text-foreground"
          >
            <span className="sr-only">{t("chat.tokenUsage.close")}</span>
            <svg
              className="size-3.5"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M6 18L18 6M6 6l12 12"
              />
            </svg>
          </button>
        </div>

        {/* Cumulative actual context usage */}
        <div className="mb-3">
          <div className="mb-1.5 flex justify-between text-xs">
            <span className="text-muted-foreground">
              {t("chat.tokenUsage.latestContextCall")}
            </span>
            <div className="flex items-center gap-1.5">
              {latest.isOverflow && (
                <span className="rounded bg-amber-500/10 px-1.5 py-0.5 font-medium text-amber-600 dark:text-amber-400">
                  {t("chat.tokenUsage.overflow")}
                </span>
              )}
              <span className="font-medium text-foreground">
                {totalTokensUsed.toLocaleString()} /{" "}
                {latest.contextWindowTokens.toLocaleString()}
              </span>
            </div>
          </div>
          {renderTotalProgress()}
          {latest.isOverflow && latest.hardBudgetTokens !== null && (
            <div className="mt-1.5 text-xs text-amber-600 dark:text-amber-400">
              {t("chat.tokenUsage.hardInputBudgetOverflow", {
                budget: latest.hardBudgetTokens.toLocaleString(),
                excess: (
                  latest.contextInputTokens - latest.hardBudgetTokens
                ).toLocaleString(),
              })}
            </div>
          )}
        </div>

        {renderStepCompression(latest)}

        {/* Legend */}
        <div className="mb-3 flex items-center justify-between text-xs">
          <div className="flex items-center gap-4">
            <div className="flex items-center gap-1.5">
              <span className="size-2.5 rounded-sm bg-blue-500" />
              <span className="text-muted-foreground">
                {t("chat.tokenUsage.input")}
              </span>
            </div>
            <div className="flex items-center gap-1.5">
              <span className="size-2.5 rounded-sm bg-amber-500" />
              <span className="text-muted-foreground">
                {t("chat.tokenUsage.output")}
              </span>
            </div>
          </div>
          {previous.length > 0 ? (
            <Popover
              open={stepHistoryOpen}
              onOpenChange={(open) => {
                setStepHistoryOpen(open);
                if (!open) setStepHistoryPinned(false);
              }}
            >
              <PopoverTrigger asChild>
                <button
                  type="button"
                  aria-expanded={stepHistoryOpen}
                  onMouseEnter={openStepHistoryPreview}
                  onMouseLeave={scheduleStepHistoryClose}
                  onClick={toggleStepHistoryPinned}
                  className="rounded bg-primary/10 px-1.5 py-0.5 font-medium text-primary"
                >
                  {t("chat.tokenUsage.steps", { count: stepCount })}
                </button>
              </PopoverTrigger>
              <PopoverContent
                side="top"
                align="end"
                collisionPadding={12}
                onMouseEnter={cancelStepHistoryClose}
                onMouseLeave={scheduleStepHistoryClose}
                className="w-80 p-3"
              >
                <div className="custom-scrollbar max-h-56 space-y-3 overflow-y-auto">
                  {previous.map((stepUsage) => (
                    <div
                      key={stepUsage.step.stepNumber}
                      className="space-y-1.5"
                    >
                      <div className="flex items-center justify-between gap-3 text-xs">
                        <span className="font-medium">
                          {stepSummary(stepUsage)}
                        </span>
                        <span className="shrink-0 text-muted-foreground">
                          {stepUsage.contextInputTokens.toLocaleString()} /{" "}
                          {stepUsage.contextWindowTokens.toLocaleString()}
                        </span>
                      </div>
                      {renderStepProgress(stepUsage)}
                      {renderStepCompression(stepUsage, true)}
                    </div>
                  ))}
                </div>
              </PopoverContent>
            </Popover>
          ) : (
            <span className="rounded bg-primary/10 px-1.5 py-0.5 font-medium text-primary">
              {t("chat.tokenUsage.steps", { count: stepCount })}
            </span>
          )}
        </div>

        {/* Turn details */}
        {(cumulativeSavedTokens > 0 || summaryGenerationCostTokens > 0) && (
          <div className="space-y-2 border-t border-border pt-2 text-xs">
            {cumulativeSavedTokens > 0 && (
              <div
                className="flex items-center justify-between"
                title={t("chat.tokenUsage.cumulativeSavedHelp")}
              >
                <span className="font-medium text-muted-foreground">
                  {t("chat.tokenUsage.cumulativeSaved")}
                </span>
                <span className="font-medium text-emerald-600 dark:text-emerald-400">
                  {cumulativeSavedTokens.toLocaleString()} Token
                </span>
              </div>
            )}
            {summaryGenerationCostTokens > 0 && (
              <div className="flex items-center justify-between">
                <span className="font-medium text-muted-foreground">
                  {t("chat.tokenUsage.summaryGenerationCost")}
                </span>
                <span className="font-medium text-foreground">
                  {summaryGenerationCostTokens.toLocaleString()} Token
                </span>
              </div>
            )}
          </div>
        )}
      </PopoverContent>
    </Popover>
  );
};

SingleTurnTokenUsage.displayName = "SingleTurnTokenUsage";
